package com.amazon.sample.orders;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.amazon.sample.orders.config.messaging.RabbitMQEnvironmentListener;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.bootstrap.DefaultBootstrapContext;
import org.springframework.boot.context.event.ApplicationEnvironmentPreparedEvent;
import org.springframework.context.event.ContextClosedEvent;
import org.springframework.core.ResolvableType;
import org.springframework.core.env.MapPropertySource;
import org.springframework.core.env.StandardEnvironment;

public class RabbitMQEnvironmentListenerTests {

  @Test
  void mapsRabbitCredentialsOnlyDuringEnvironmentPreparation() {
    var environment = new StandardEnvironment();
    environment
      .getPropertySources()
      .addFirst(
        new MapPropertySource(
          "test",
          Map.of(
            "retail.orders.messaging.provider",
            "rabbitmq",
            "retail.orders.messaging.rabbitmq.addresses",
            "amqps://broker.internal:5671",
            "retail.orders.messaging.rabbitmq.username",
            "orders-dev",
            "retail.orders.messaging.rabbitmq.password",
            "ci-only-password"
          )
        )
      );
    var listener = new RabbitMQEnvironmentListener();
    assertTrue(
      listener.supportsEventType(
        ResolvableType.forClass(ApplicationEnvironmentPreparedEvent.class)
      )
    );
    assertFalse(
      listener.supportsEventType(
        ResolvableType.forClass(ContextClosedEvent.class)
      )
    );
    listener.onApplicationEvent(
      new ApplicationEnvironmentPreparedEvent(
        new DefaultBootstrapContext(),
        new SpringApplication(OrdersApplication.class),
        new String[0],
        environment
      )
    );
    assertEquals(
      "amqps://broker.internal:5671",
      environment.getProperty("spring.rabbitmq.addresses")
    );
    assertEquals(
      "orders-dev",
      environment.getProperty("spring.rabbitmq.username")
    );
    assertEquals(
      "ci-only-password",
      environment.getProperty("spring.rabbitmq.password")
    );
  }
}
