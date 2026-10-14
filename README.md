### Python Version ###
3.14

---

### SET UP DATABASE ###

**Windows**

```sql
psql -U postgres -h localhost
```

Then paste:

```sql
CREATE DATABASE machine_db;
CREATE USER machine WITH PASSWORD '2+PJh#&?';
ALTER ROLE machine SET client_encoding TO 'utf8';
ALTER ROLE machine SET default_transaction_isolation TO 'read committed';
ALTER ROLE machine SET timezone TO 'UTC';
GRANT ALL PRIVILEGES ON DATABASE machine_db TO machine;
\c machine_db;
ALTER SCHEMA public OWNER TO machine;
GRANT ALL ON SCHEMA public TO machine;
\q
```