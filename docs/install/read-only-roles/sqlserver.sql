-- carto read-only login for SQL Server (spec 8.1.4, Appendix C).
-- Run as sysadmin. Connect carto through the availability group listener with
-- ApplicationIntent=ReadOnly so reads route to a readable secondary (carto sets it).

CREATE LOGIN carto_reader WITH PASSWORD = 'REPLACE_WITH_GENERATED_PASSWORD', CHECK_POLICY = ON;
USE wms;                                                     -- REPLACE database
CREATE USER carto_reader FOR LOGIN carto_reader;
GRANT SELECT ON OBJECT::dbo.purchase_orders TO carto_reader; -- REPLACE; prefer a view with only the needed columns

-- Verify (carto checks HAS_PERMS_BY_NAME for INSERT/UPDATE/DELETE/ALTER and IS_SRVROLEMEMBER('sysadmin')):
--   EXECUTE AS USER = 'carto_reader';
--   SELECT HAS_PERMS_BY_NAME('dbo.purchase_orders', 'OBJECT', 'INSERT');  -- must be 0
--   REVERT;
