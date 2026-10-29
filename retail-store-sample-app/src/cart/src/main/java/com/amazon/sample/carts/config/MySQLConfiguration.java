package com.amazon.sample.carts.config;

import com.amazon.sample.carts.services.MySQLCartService;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import javax.sql.DataSource;
import org.flywaydb.core.Flyway;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

@Configuration
@ConditionalOnProperty(prefix = "retail.cart.persistence", name = "provider", havingValue = "mysql")
public class MySQLConfiguration {

  @Bean(destroyMethod = "close")
  HikariDataSource cartDataSource(
    @Value("${retail.cart.persistence.endpoint}") String endpoint,
    @Value("${retail.cart.persistence.database}") String database,
    @Value("${retail.cart.persistence.username}") String username,
    @Value("${retail.cart.persistence.password}") String password,
    @Value("${retail.cart.persistence.ssl-mode:VERIFY_IDENTITY}") String sslMode,
    @Value("${retail.cart.persistence.maximum-pool-size:10}") int poolSize,
    @Value("${retail.cart.persistence.trust-store-url:}") String trustStore
  ) {
    HikariConfig config = new HikariConfig();
    config.setJdbcUrl("jdbc:mysql://" + endpoint + "/" + database);
    config.setUsername(username);
    config.setPassword(password);
    config.addDataSourceProperty("sslMode", sslMode);
    if (!trustStore.isBlank()) {
      config.addDataSourceProperty("trustCertificateKeyStoreUrl", trustStore);
      config.addDataSourceProperty("trustCertificateKeyStoreType", "PKCS12");
      config.addDataSourceProperty("trustCertificateKeyStorePassword", "changeit");
    }
    config.setMaximumPoolSize(poolSize);
    config.setConnectionTimeout(10000);
    return new HikariDataSource(config);
  }

  @Bean
  MySQLCartService mysqlCartService(DataSource cartDataSource) {
    Flyway.configure().dataSource(cartDataSource).locations("classpath:db/mysql").load().migrate();
    return new MySQLCartService(
      new JdbcTemplate(cartDataSource),
      new TransactionTemplate(new DataSourceTransactionManager(cartDataSource))
    );
  }
}
