package com.amazon.sample.orders.messaging.rabbitmq;

import com.amazon.sample.events.orders.OrderCreatedEvent;
import com.amazon.sample.orders.config.messaging.RabbitMQMessagingConfig;
import com.amazon.sample.orders.messaging.MessagingProvider;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import lombok.extern.slf4j.Slf4j;
import org.springframework.amqp.AmqpException;
import org.springframework.amqp.AmqpRejectAndDontRequeueException;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.ObjectMapper;

/** Transactional outbox and idempotent event receipt for the MySQL release profile. */
@Slf4j
public class MySQLOrderEvents implements MessagingProvider {
  private final RabbitTemplate rabbit;
  private final JdbcTemplate jdbc;
  private final TransactionTemplate transactions;
  private final ObjectMapper mapper = new ObjectMapper();

  public MySQLOrderEvents(RabbitTemplate rabbit, JdbcTemplate jdbc) {
    this.rabbit = rabbit;
    this.jdbc = jdbc;
    this.transactions = new TransactionTemplate(new DataSourceTransactionManager(jdbc.getDataSource()));
  }

  @Override
  public void publishEvent(Object value) {
    if (!(value instanceof OrderCreatedEvent event)) throw new IllegalArgumentException("Expected OrderCreatedEvent");
    String eventId = UUID.randomUUID().toString();
    String payload = mapper.writeValueAsString(Map.of("eventId", eventId,
      "orderId", event.getOrder().getId(), "order", event.getOrder()));
    // BEFORE_COMMIT: order and outbox row share the same database transaction.
    jdbc.update("INSERT INTO order_outbox(event_id, payload) VALUES (?, ?)", eventId, payload);
  }

  @Scheduled(fixedDelayString = "${retail.orders.messaging.outbox-delay-ms:1000}")
  public void dispatch() {
    try {
      transactions.executeWithoutResult(status -> {
        var pending = jdbc.queryForList("SELECT event_id, payload FROM order_outbox WHERE sent_at IS NULL "
          + "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED");
        if (pending.isEmpty()) return;
        var row = pending.get(0);
        String id = row.get("event_id").toString();
        MessageProperties properties = new MessageProperties();
        properties.setContentType(MessageProperties.CONTENT_TYPE_JSON);
        properties.setMessageId(id);
        properties.setDeliveryMode(MessageDeliveryMode.PERSISTENT);
        CorrelationData correlation = new CorrelationData(id);
        rabbit.send(RabbitMQMessagingConfig.EXCHANGE_NAME, "orders.created",
          new Message(row.get("payload").toString().getBytes(StandardCharsets.UTF_8), properties), correlation);
        try {
          var confirm = correlation.getFuture().get(5, TimeUnit.SECONDS);
          if (!confirm.ack() || correlation.getReturned() != null) throw new AmqpException("Order event was not routed and confirmed");
        } catch (InterruptedException error) {
          Thread.currentThread().interrupt();
          throw new AmqpException("Interrupted waiting for broker confirm", error);
        } catch (java.util.concurrent.ExecutionException | java.util.concurrent.TimeoutException error) {
          throw new AmqpException("Broker confirm failed; outbox event retained", error);
        }
        jdbc.update("UPDATE order_outbox SET sent_at = CURRENT_TIMESTAMP(6) WHERE event_id = ?", id);
      });
    } catch (RuntimeException error) {
      log.warn("Order event remains in outbox for retry: {}", error.getClass().getSimpleName());
    }
  }

  @RabbitListener(queues = RabbitMQMessagingConfig.ORDERS_ORDERS_QUEUE)
  public void receive(Message message) {
    String eventId;
    String orderId;
    try {
      var event = mapper.readTree(message.getBody());
      eventId = UUID.fromString(event.path("eventId").asText()).toString();
      orderId = UUID.fromString(event.path("orderId").asText()).toString();
      if (!eventId.equals(message.getMessageProperties().getMessageId())) throw new IllegalArgumentException("Event identity mismatch");
    } catch (RuntimeException error) {
      throw new AmqpRejectAndDontRequeueException("Invalid order event", error);
    }
    for (int attempt = 0; attempt < 3; attempt++) {
      try {
        // Durable receipt is this sample consumer's effect. Replays keep one receipt.
        jdbc.update("INSERT INTO consumed_order_events(event_id, order_id) VALUES (?, ?) "
          + "ON DUPLICATE KEY UPDATE event_id = event_id", eventId, orderId);
        return;
      } catch (org.springframework.dao.DataAccessException error) {
        if (attempt == 2) throw new AmqpRejectAndDontRequeueException("Order event exhausted retries", error);
        try {
          Thread.sleep(1000L * (attempt + 1));
        } catch (InterruptedException interrupted) {
          Thread.currentThread().interrupt();
          throw new AmqpRejectAndDontRequeueException("Interrupted order event", interrupted);
        }
      }
    }
  }
}
