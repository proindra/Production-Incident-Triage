CREATE ROLE incident_readonly LOGIN;
GRANT CONNECT ON DATABASE incidentdb TO incident_readonly;
GRANT pg_monitor TO incident_readonly;
