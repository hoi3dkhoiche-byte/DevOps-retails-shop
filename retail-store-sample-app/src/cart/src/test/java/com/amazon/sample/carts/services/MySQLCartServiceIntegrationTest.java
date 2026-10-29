package com.amazon.sample.carts.services;

import static org.junit.jupiter.api.Assertions.*;

import java.util.concurrent.Executors;
import java.util.stream.IntStream;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.mysql.MySQLContainer;

@SpringBootTest(properties = "retail.cart.persistence.provider=mysql")
@Testcontainers
@Tag("integration")
public class MySQLCartServiceIntegrationTest extends AbstractServiceTests {

  @Container
  static MySQLContainer mysql = new MySQLContainer("mysql:8.4.11");

  @DynamicPropertySource
  static void properties(DynamicPropertyRegistry registry) {
    registry.add("retail.cart.persistence.endpoint", () -> mysql.getHost() + ":" + mysql.getMappedPort(3306));
    registry.add("retail.cart.persistence.database", mysql::getDatabaseName);
    registry.add("retail.cart.persistence.username", mysql::getUsername);
    registry.add("retail.cart.persistence.password", mysql::getPassword);
    registry.add("retail.cart.persistence.ssl-mode", () -> "DISABLED");
  }

  @Autowired
  private MySQLCartService service;

  @Override
  public CartService getService() {
    return service;
  }

  @Test
  void concurrentAddsDoNotLoseQuantities() throws Exception {
    try (var executor = Executors.newFixedThreadPool(4)) {
      var futures = IntStream.range(0, 20)
        .mapToObj(i -> executor.submit(() -> service.add("concurrent", "shoe", 2, 100))).toList();
      for (var future : futures) future.get();
    }
    assertEquals(40, service.item("concurrent", "shoe").orElseThrow().getQuantity());
  }

  @Test
  void mergePreservesItemsAndRemovesSession() {
    service.add("session", "shoe", 2, 100);
    service.add("customer", "shoe", 3, 100);
    assertEquals(5, service.merge("session", "customer").getItems().get(0).getQuantity());
    assertFalse(service.exists("session"));
    assertEquals(5, service.merge("customer", "customer").getItems().get(0).getQuantity());
  }

  @Test
  void updateAndDeleteAreIsolatedByCustomer() {
    service.add("owner", "shoe", 1, 100);
    service.add("other", "shoe", 2, 100);
    assertEquals(7, service.update("owner", "shoe", 7, 250).orElseThrow().getQuantity());
    service.delete("owner");
    assertTrue(service.item("owner", "shoe").isEmpty());
    assertEquals(2, service.item("other", "shoe").orElseThrow().getQuantity());
  }
}
