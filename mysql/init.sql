-- =====================================================================
-- FastSense · Modelo de dominio (MySQL 8.0)
-- 4 entidades: USUARIO, PRODUCTO, INTERACCION, MODELO_VERSION
-- Sin PII en texto plano: email_hash = SHA-256.
-- =====================================================================
CREATE DATABASE IF NOT EXISTS fastsense
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE fastsense;

CREATE TABLE IF NOT EXISTS USUARIO (
  user_id     VARCHAR(64)  NOT NULL,
  email_hash  CHAR(64)     NOT NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  segment     VARCHAR(32)  NOT NULL DEFAULT 'regular',
  PRIMARY KEY (user_id),
  KEY idx_usuario_segment (segment),
  KEY idx_usuario_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS PRODUCTO (
  product_id  VARCHAR(64)   NOT NULL,
  name        VARCHAR(255)  NOT NULL,
  category    VARCHAR(64)   NOT NULL,
  price       DECIMAL(10,2) NOT NULL,
  stock       INT           NOT NULL DEFAULT 0,
  attributes  JSON          NULL,
  PRIMARY KEY (product_id),
  KEY idx_producto_category (category),
  KEY idx_producto_price (price)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS INTERACCION (
  interaction_id   BIGINT       NOT NULL AUTO_INCREMENT,
  user_id          VARCHAR(64)  NOT NULL,
  product_id       VARCHAR(64)  NOT NULL,
  event_type       VARCHAR(16)  NOT NULL,
  rating_value     DECIMAL(3,2) NULL,
  `timestamp`      DATETIME(3)  NOT NULL,
  session_id       VARCHAR(64)  NULL,
  idempotency_key  VARCHAR(96)  NOT NULL,
  created_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (interaction_id),
  UNIQUE KEY uq_interaccion_idempotency (idempotency_key),
  KEY idx_interaccion_user_time (user_id, `timestamp`),
  KEY idx_interaccion_product_time (product_id, `timestamp`),
  KEY idx_interaccion_event (event_type),
  CONSTRAINT chk_interaccion_event
    CHECK (event_type IN ('click','cart','purchase','rating'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS MODELO_VERSION (
  model_id        BIGINT       NOT NULL AUTO_INCREMENT,
  version         VARCHAR(64)  NOT NULL,
  rmse            DECIMAL(5,4) NOT NULL,
  precision_at_10 DECIMAL(5,4) NOT NULL,
  s3_uri          VARCHAR(512) NOT NULL,
  trained_at      DATETIME     NOT NULL,
  status          VARCHAR(16)  NOT NULL DEFAULT 'active',
  PRIMARY KEY (model_id),
  UNIQUE KEY uq_modelo_version (version),
  KEY idx_modelo_status (status),
  KEY idx_modelo_trained (trained_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
