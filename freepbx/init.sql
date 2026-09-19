-- EMPTY DATABASE VOLUME ONLY: MariaDB's entrypoint creates freepbxuser@'%'
-- from MYSQL_USER + MYSQL_PASSWORD_FILE before processing this file.
-- Both databases are explicit here; no MYSQL_DATABASE default is required.
-- Never create a passwordless user here or commit an IDENTIFIED BY credential.
CREATE DATABASE IF NOT EXISTS `asterisk`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS `asteriskcdrdb`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- The account host is the connecting PBX, NOT the server name 'db' and NOT
-- localhost. '%' works across the private Compose network; 3306 is unpublished.
-- Keep unrelated containers off that network; grants are limited to these DBs.
GRANT ALL PRIVILEGES ON `asterisk`.* TO 'freepbxuser'@'%';
GRANT ALL PRIVILEGES ON `asteriskcdrdb`.* TO 'freepbxuser'@'%';
-- GRANT takes effect immediately; no FLUSH PRIVILEGES or global privileges.
