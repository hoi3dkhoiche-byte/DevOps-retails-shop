package com.amazon.sample.orders.config.messaging;

import com.amazon.sample.orders.messaging.MessagingProvider;
import com.amazon.sample.orders.messaging.rabbitmq.MySQLOrderEvents;
import com.amazon.sample.orders.messaging.rabbitmq.RabbitMQMessagingProvider;
import org.springframework.amqp.core.*;
import org.springframework.amqp.rabbit.annotation.EnableRabbit;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.support.converter.JacksonJsonMessageConverter;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.amqp.autoconfigure.RabbitAutoConfiguration;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.EnableScheduling;

@Configuration
@EnableRabbit
@EnableScheduling
@Import(RabbitAutoConfiguration.class)
@EnableConfigurationProperties(RabbitMQProperties.class)
@ConditionalOnProperty(prefix = MessagingProperties.PREFIX, name = "provider", havingValue = "rabbitmq")
public class RabbitMQMessagingConfig {
  public static final String EXCHANGE_NAME = "orders.events";
  public static final String ORDERS_ORDERS_QUEUE = "orders.events.v2";

  @Bean
  Declarables orderTopology() {
    TopicExchange exchange = new TopicExchange(EXCHANGE_NAME, true, false);
    TopicExchange deadExchange = new TopicExchange("orders.dlx", true, false);
    Queue queue = QueueBuilder.durable(ORDERS_ORDERS_QUEUE).quorum()
      .deadLetterExchange("orders.dlx").deadLetterRoutingKey("orders.failed").build();
    Queue deadQueue = QueueBuilder.durable("orders.events.dlq").quorum().build();
    return new Declarables(exchange, deadExchange, queue, deadQueue,
      BindingBuilder.bind(queue).to(exchange).with("orders.created"),
      BindingBuilder.bind(deadQueue).to(deadExchange).with("orders.failed"));
  }

  @Bean
  @Primary
  RabbitTemplate customRabbitTemplate(ConnectionFactory factory) {
    RabbitTemplate template = new RabbitTemplate(factory);
    template.setMessageConverter(new JacksonJsonMessageConverter());
    template.setMandatory(true);
    return template;
  }

  @Bean
  MessagingProvider messagingProvider(RabbitTemplate template, JdbcTemplate jdbc,
    @Value("${retail.orders.persistence.provider}") String provider) {
    if ("mysql".equals(provider)) return new MySQLOrderEvents(template, jdbc);
    return new RabbitMQMessagingProvider(template);
  }
}
