-- carto read-only login for MySQL 8 (spec 8.1.4, Appendix C).
-- Run as an administrator on the read replica. Replace the password, database and tables.

CREATE USER 'carto_reader'@'%' IDENTIFIED BY 'REPLACE_WITH_GENERATED_PASSWORD';
GRANT SELECT ON wms.purchase_orders TO 'carto_reader'@'%';   -- REPLACE; prefer a view with only the needed columns
ALTER USER 'carto_reader'@'%' REQUIRE SSL;
FLUSH PRIVILEGES;

-- Verify (carto parses SHOW GRANTS and refuses INSERT, UPDATE, DELETE, DROP, ALTER, CREATE or ALL):
--   SHOW GRANTS FOR 'carto_reader'@'%';
