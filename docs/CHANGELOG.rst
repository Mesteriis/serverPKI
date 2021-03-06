=========
Changelog
=========

.. toctree::

0.9.0 (2017-07-18)
------------------

- Initial public release.

0.9.1 (2017-07-28)
------------------

- Documentation at https://serverpki.readthedocs.io

0.9.2 (2018-03-19)
------------------

- Python 3.6 supported
- Omit disabled certs from list of certs to be renewed.
- BUGFIX: Bind place to jail not to disthost (disthost->jail->place) 
- Do not expire certs one day before "not_after" but one day after instead
- Allow "distribute only" with --renew-local-certs
- New Feature: --renew-local-certs REMAINING_DAYS 
    Renews local certs, which would expire within REMAINING_DAYS.
    Gives a nice tabular display of affected certs
- New Feature: Allow encrypted storage of keys in DB

    2 new action commands: --encrypt-keys and --decrypt-keys
    
    New configuration parameter: db_encryption_key

- Upgrading:
    Create new table Revision in DB - see install/create_schema_pki.sql::
    
     pki_op=# CREATE TABLE Revision (
     id                SERIAL          PRIMARY KEY,            -- 'PK of Revision'
     schemaVersion     int2            NOT NULL  DEFAULT 1,    -- 'Version of DB schema'
     keysEncrypted     BOOLEAN         NOT NULL  DEFAULT FALSE -- 'Cert keys are encrypted'
     );
     pki_op=# INSERT INTO revision (schemaVersion) values(1);
    
    Then create passphrase and encrypt DB (see tutorial).


0.9.3 (2019-02-11)
------------------

- Python 3.7 supported
- With pyopenssl 19  on FreeBSD 12 (which has OpenSSL 1.1.1a-freebsd in base
  system), paramiko 2.4 works out-of-the-box. No longer need for paramiko
  workarounds like package paramiko-clc.
- Now recovering from "Letsencrypt forgetting authorizations", which happened
  at begin of 2019.
- Fixing bug where one letsencrypt authorization was requested multiple times
  (happened once per distribution target).
- Being more patient with Letsencrypt's response to challenges


0.9.4 (2019-02-21)
------------------

- INCOMPATIBLE CHANGE in configuration file syntax: dbAccounts keyword has been
  changed from 'pki_dev' to 'serverpki'. See install example_config.py
- Multiple local CA certs for CA cert roll over
- Increased hash size to 512 (CA cert) resp. 384 bits (server/client cert)
- Cert (including CA cert) export by cert serial number implemented.
- Listing of cert meta info now also lists (issued) cert instances.
- requirement for PyOpenSSL removed.
- BUGFIXES e.g. Allow to enter 1st cert into empty CertInstances table

0.9.6 (2020-03-11)
------------------

- Supporting and (requiring) V2 of ACME protocoll.
- New fields in DB for upcoming support of certs with elliptic algorithm.
  (in addition to rsa). Run install/upgrade_to_2.sql in psql, connected to pki DB.

0.9.10 (2020-08-06)
-------------------

- New object oriented architecture, abstracting relational model
- Support for dynamic DNS update mode of operation supported
- Support for dual algo certs (rsa + ec)
- Support for OCSP_must_staple attribute
- New config file format
- BUGFIXES mainly in ACMEv2 handshaking code
- For upgrade run install/upgrade_to_{3456}.sql in psql, connected to pki DB.


0.9.11 (2020-08-11)
-------------------

- Using automatoes 0.9.5. Got hotfix from automatoes maintainer