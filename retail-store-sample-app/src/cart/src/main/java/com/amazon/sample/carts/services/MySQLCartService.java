package com.amazon.sample.carts.services;

import com.amazon.sample.carts.repositories.CartEntity;
import com.amazon.sample.carts.repositories.ItemEntity;
import java.util.List;
import java.util.Optional;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;

/** MySQL persistence; mutations lock the cart so concurrent requests cannot lose items. */
public class MySQLCartService implements CartService {

  private final JdbcTemplate jdbc;
  private final TransactionTemplate transactions;

  public MySQLCartService(JdbcTemplate jdbc, TransactionTemplate transactions) {
    this.jdbc = jdbc;
    this.transactions = transactions;
  }

  private void createCart(String customerId) {
    jdbc.update("INSERT INTO carts (customer_id) VALUES (?) ON DUPLICATE KEY UPDATE customer_id = customer_id", customerId);
  }

  private void lockCart(String customerId) {
    createCart(customerId);
    jdbc.queryForObject("SELECT customer_id FROM carts WHERE customer_id = ? FOR UPDATE", String.class, customerId);
  }

  @Override
  public CartEntity get(String customerId) {
    createCart(customerId);
    Cart cart = new Cart(customerId);
    cart.setItems(items(customerId));
    return cart;
  }

  @Override
  public void delete(String customerId) {
    transactions.executeWithoutResult(status -> jdbc.update("DELETE FROM carts WHERE customer_id = ?", customerId));
  }

  @Override
  public CartEntity merge(String sessionId, String customerId) {
    return transactions.execute(status -> {
      if (!sessionId.equals(customerId)) {
        // Consistent lock order prevents opposite-direction merges from deadlocking.
        lockCart(sessionId.compareTo(customerId) < 0 ? sessionId : customerId);
        lockCart(sessionId.compareTo(customerId) < 0 ? customerId : sessionId);
        for (CartItem item : items(sessionId)) {
          add(customerId, item.getItemId(), item.getQuantity(), item.getUnitPrice());
        }
        delete(sessionId);
      }
      return get(customerId);
    });
  }

  @Override
  public ItemEntity add(String customerId, String itemId, int quantity, int unitPrice) {
    return transactions.execute(status -> {
      lockCart(customerId);
      jdbc.update("INSERT INTO cart_items (customer_id, item_id, quantity, unit_price) VALUES (?, ?, ?, ?) "
        + "ON DUPLICATE KEY UPDATE quantity = quantity + ?, unit_price = ?",
        customerId, itemId, quantity, unitPrice, quantity, unitPrice);
      return item(customerId, itemId).orElseThrow();
    });
  }

  @Override
  public List<CartItem> items(String customerId) {
    return jdbc.query("SELECT item_id, quantity, unit_price FROM cart_items WHERE customer_id = ? ORDER BY item_id",
      (row, number) -> new CartItem(row.getString(1), row.getInt(2), row.getInt(3)), customerId);
  }

  @Override
  public Optional<CartItem> item(String customerId, String itemId) {
    return jdbc.query("SELECT item_id, quantity, unit_price FROM cart_items WHERE customer_id = ? AND item_id = ?",
      (row, number) -> new CartItem(row.getString(1), row.getInt(2), row.getInt(3)), customerId, itemId).stream().findFirst();
  }

  @Override
  public void deleteItem(String customerId, String itemId) {
    transactions.executeWithoutResult(status -> {
      lockCart(customerId);
      jdbc.update("DELETE FROM cart_items WHERE customer_id = ? AND item_id = ?", customerId, itemId);
    });
  }

  @Override
  public Optional<CartItem> update(String customerId, String itemId, int quantity, int unitPrice) {
    return transactions.execute(status -> {
      lockCart(customerId);
      jdbc.update("UPDATE cart_items SET quantity = ?, unit_price = ? WHERE customer_id = ? AND item_id = ?",
        quantity, unitPrice, customerId, itemId);
      return item(customerId, itemId);
    });
  }

  @Override
  public boolean exists(String customerId) {
    return jdbc.queryForObject("SELECT COUNT(*) FROM carts WHERE customer_id = ?", Integer.class, customerId) > 0;
  }
}
