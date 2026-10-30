package com.amazon.sample.orders.config.messaging;

import java.util.Properties;
import org.springframework.boot.context.event.ApplicationEnvironmentPreparedEvent;
import org.springframework.context.ApplicationEvent;
import org.springframework.context.event.GenericApplicationListener;
import org.springframework.core.ResolvableType;
import org.springframework.core.env.PropertiesPropertySource;

public class RabbitMQEnvironmentListener implements GenericApplicationListener {

  @Override
  public boolean supportsEventType(ResolvableType eventType) {
    return ApplicationEnvironmentPreparedEvent.class.isAssignableFrom(
      eventType.toClass()
    );
  }

  @Override
  public void onApplicationEvent(ApplicationEvent event) {
    if (!(event instanceof ApplicationEnvironmentPreparedEvent prepared)) {
      return;
    }
    var environment = prepared.getEnvironment();

    var messagingProvider = environment.getProperty(
      "retail.orders.messaging.provider"
    );

    if ("rabbitmq".equals(messagingProvider)) {
      Properties props = new Properties();
      props.put(
        "spring.rabbitmq.addresses",
        environment.getProperty("retail.orders.messaging.rabbitmq.addresses")
      );
      props.put(
        "spring.rabbitmq.username",
        environment.getProperty("retail.orders.messaging.rabbitmq.username")
      );
      props.put(
        "spring.rabbitmq.password",
        environment.getProperty("retail.orders.messaging.rabbitmq.password")
      );
      environment
        .getPropertySources()
        .addFirst(new PropertiesPropertySource("rabbitmqProps", props));
    }
  }
}
