package com.amazon.sample.orders.config;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
@ConditionalOnProperty(
  prefix = DatabaseProperties.PREFIX,
  name = "provider",
  havingValue = "mysql"
)
@EnableConfigurationProperties(DatabaseProperties.class)
public class MySQLDataSourceConfig {

  private static final long CONNECTION_TIMEOUT_MS = 10000L;

  @Bean(destroyMethod = "close")
  public HikariDataSource dataSource(
    DatabaseProperties properties,
    @Value(
      "${retail.orders.persistence.ssl-mode:VERIFY_IDENTITY}"
    ) String sslMode,
    @Value("${retail.orders.persistence.maximum-pool-size:10}") int poolSize,
    @Value("${retail.orders.persistence.trust-store-url:}") String trustStore
  ) {
    HikariConfig config = new HikariConfig();
    config.setJdbcUrl(
      "jdbc:mysql://" + properties.getEndpoint() + "/" + properties.getName()
    );
    config.setUsername(properties.getUsername());
    config.setPassword(properties.getPassword());
    config.addDataSourceProperty("sslMode", sslMode);
    if (!trustStore.isBlank()) {
      config.addDataSourceProperty("trustCertificateKeyStoreUrl", trustStore);
      config.addDataSourceProperty("trustCertificateKeyStoreType", "PKCS12");
      config.addDataSourceProperty(
        "trustCertificateKeyStorePassword",
        "changeit"
      );
    }
    config.setMaximumPoolSize(poolSize);
    config.setConnectionTimeout(CONNECTION_TIMEOUT_MS);
    return new HikariDataSource(config);
  }
}
