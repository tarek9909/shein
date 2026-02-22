-- FastAPI tables in shared database (separate from PHP app tables)
-- Safe to run multiple times.

CREATE TABLE IF NOT EXISTS `shein_api_users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `owner_user_id` int(11) DEFAULT NULL,
  `email` varchar(255) NOT NULL,
  `gmail_email` varchar(255) NOT NULL,
  `gmail_app_password_enc` text NOT NULL,
  `shein_email` varchar(255) NOT NULL,
  `shein_password_enc` text NOT NULL,
  `shein_storage_state_enc` text DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_shein_api_users_owner_user_id` (`owner_user_id`),
  KEY `ix_shein_api_users_email` (`email`),
  UNIQUE KEY `uq_shein_owner_email` (`owner_user_id`,`email`),
  CONSTRAINT `fk_shein_api_users_owner`
    FOREIGN KEY (`owner_user_id`) REFERENCES `users` (`id`)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `shein_api_orders` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `user_id` int(11) NOT NULL,
  `order_no` varchar(64) NOT NULL,
  `carrier` varchar(64) DEFAULT NULL,
  `tracking_no` varchar(64) DEFAULT NULL,
  `status_text` varchar(255) DEFAULT NULL,
  `delivered` tinyint(1) DEFAULT 0,
  `last_details` text DEFAULT NULL,
  `last_timestamp` varchar(64) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_user_order` (`user_id`,`order_no`),
  CONSTRAINT `fk_shein_api_orders_user`
    FOREIGN KEY (`user_id`) REFERENCES `shein_api_users` (`id`)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
