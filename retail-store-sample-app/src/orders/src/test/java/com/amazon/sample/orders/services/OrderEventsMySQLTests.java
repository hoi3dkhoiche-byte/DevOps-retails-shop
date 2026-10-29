package com.amazon.sample.orders.services;

import static org.junit.jupiter.api.Assertions.*;

import com.amazon.sample.orders.entities.OrderEntity;
import com.amazon.sample.orders.entities.OrderItemEntity;
import com.amazon.sample.orders.entities.ShippingAddressEntity;
import com.amazon.sample.orders.messaging.rabbitmq.MySQLOrderEvents;
import java.time.Duration;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.springframework.amqp.AmqpRejectAndDontRequeueException;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.mysql.MySQLContainer;
import org.testcontainers.containers.wait.strategy.Wait;

@SpringBootTest(properties = {"retail.orders.persistence.provider=mysql", "retail.orders.persistence.ssl-mode=DISABLED", "retail.orders.messaging.provider=rabbitmq"})
@Testcontainers
@Tag("integration")
public class OrderEventsMySQLTests {
  @Container
  static MySQLContainer mysql = new MySQLContainer("mysql:8.4.11");
  @Container
  static GenericContainer<?> broker = new GenericContainer<>("rabbitmq:4.2.0-management")
    .withEnv("RABBITMQ_DEFAULT_USER", "retail").withEnv("RABBITMQ_DEFAULT_PASS", "ci-only-password")
    .withExposedPorts(5672).waitingFor(Wait.forLogMessage(".*Server startup complete.*", 1).withStartupTimeout(Duration.ofMinutes(3)));

  @DynamicPropertySource
  static void properties(DynamicPropertyRegistry registry) {
    registry.add("retail.orders.persistence.endpoint", () -> mysql.getHost() + ":" + mysql.getMappedPort(3306));
    registry.add("retail.orders.persistence.name", mysql::getDatabaseName);
    registry.add("retail.orders.persistence.username", mysql::getUsername);
    registry.add("retail.orders.persistence.password", mysql::getPassword);
    // Dynamic properties are resolved after ApplicationEnvironmentPreparedEvent.
    registry.add("spring.rabbitmq.addresses", () -> broker.getHost() + ":" + broker.getMappedPort(5672));
    registry.add("spring.rabbitmq.username", () -> "retail");
    registry.add("spring.rabbitmq.password", () -> "ci-only-password");
  }

  @Autowired OrderService orders;
  @Autowired JdbcTemplate jdbc;
  @Autowired MySQLOrderEvents events;
  @Autowired RabbitTemplate rabbit;

  @Test
  void committedOrderIsConfirmedAndConsumedOnce() throws Exception {
    OrderEntity order = orders.create(new OrderEntity(List.of(new OrderItemEntity("shoe-123", 2, 100, 200)),
      new ShippingAddressEntity("Test", "Buyer", "ci@example.com", "Street", "", "City", "12345", "CA")));
    long deadline = System.nanoTime() + Duration.ofSeconds(30).toNanos();
    while (jdbc.queryForObject("SELECT COUNT(*) FROM consumed_order_events WHERE order_id = ?", Integer.class, order.getId()) == 0 && System.nanoTime() < deadline) Thread.sleep(100);
    assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM consumed_order_events WHERE order_id = ?", Integer.class, order.getId()));
    var row = jdbc.queryForMap("SELECT event_id, payload, sent_at FROM order_outbox WHERE event_id = (SELECT event_id FROM consumed_order_events WHERE order_id = ?)", order.getId());
    assertNotNull(row.get("sent_at"));
    MessageProperties properties = new MessageProperties();
    properties.setMessageId(row.get("event_id").toString());
    Message replay = new Message(row.get("payload").toString().getBytes(java.nio.charset.StandardCharsets.UTF_8), properties);
    events.receive(replay);
    events.receive(replay);
    assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM consumed_order_events WHERE order_id = ?", Integer.class, order.getId()));
  }

  @Test
  void invalidPayloadIsRejectedWithoutRequeue() {
    MessageProperties properties = new MessageProperties();
    properties.setMessageId(UUID.randomUUID().toString());
    assertThrows(AmqpRejectAndDontRequeueException.class, () -> events.receive(new Message("{}".getBytes(), properties)));
  }
}
