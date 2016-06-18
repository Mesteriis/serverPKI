----------------------------- serverPKI data dictionary schema
SET log_min_messages='error';

-- 'Attention!! drops referencing schema pki  Attention!!'
DROP SCHEMA IF EXISTS dd CASCADE;


CREATE SCHEMA dd;            -- 'data dictionary for project serverPKI




------------------------------ 'created' timestamp
CREATE DOMAIN dd.created
    AS TIMESTAMP
    DEFAULT TIMESTAMP 'NOW'
    NOT NULL;
ALTER DOMAIN dd.created OWNER TO pki_dev;

------------------------------ 'updated' timestamp
CREATE DOMAIN dd.updated
    AS TIMESTAMP
    DEFAULT TIMESTAMP 'NOW'
    NOT NULL;
ALTER DOMAIN dd.updated OWNER TO pki_dev;

------------------------------ TCP/UDP port number number
CREATE DOMAIN dd.port_number
    AS int4
    CONSTRAINT port_number CHECK (VALUE >= 0 AND VALUE <= 65536);
ALTER DOMAIN dd.port_number OWNER TO pki_dev;

------------------------------ type of Subject
CREATE TYPE dd.subject_type AS ENUM ('client', 'server');
ALTER TYPE dd.subject_type OWNER TO pki_dev;

------------------------------ type of Place
CREATE TYPE dd.place_cert_file_type AS ENUM (
    'separate', 'combined cert key', 'combined cacert cert key');
ALTER TYPE dd.subject_type OWNER TO pki_dev;



GRANT USAGE ON SCHEMA dd TO pki_dev;