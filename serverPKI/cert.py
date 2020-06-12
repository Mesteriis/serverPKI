# -*- coding: utf-8 -*-

"""
Copyright (C) 2015-2020  Axel Rau <axel.rau@chaos1.de>

This file is part of serverPKI.

serverPKI is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Foobar is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with serverPKI.  If not, see <http://www.gnu.org/licenses/>.
"""

# Certificate class module

# --------------- imported modules --------------
import binascii
import datetime
from functools import total_ordering
from pathlib import Path
import sys
from typing import Union, Optional, Dict, List, Tuple

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPrivateKeyWithSerialization
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    Encoding,
    PrivateFormat,
    NoEncryption,
    BestAvailableEncryption
)
from cryptography import x509

from postgresql import driver as db_conn
# --------------- local imports --------------
from serverPKI.db import DBStoreException
from serverPKI.utils import Pathes, sld, sli, sln, sle, db_encryption_key, db_encryption_in_use

# ---------------  prepared SQL queries for class Certificate  --------------

q_all_cert_meta = """
 SELECT s1.type AS subject_type,
    c.id AS c_id,
    c.disabled AS c_disabled,
    c.type AS c_type,
    c.authorized_until AS authorized_until,
    c.encryption_algo AS encryption_algo,
    c.ocsp_must_staple AS ocsp_must_staple,
    s2.name AS alt_name,
    s.tlsaprefix AS tlsaprefix,
    d.fqdn AS dist_host,
    d.jailroot AS jailroot,
    j.name AS jail,
    p.name AS place,
    p.cert_file_type AS cert_file_type,
    p.cert_path AS cert_path,
    p.key_path AS key_path,
    p.uid AS uid,
    p.gid AS gid,
    p.mode AS mode,
    p.chownBoth AS chownboth,
    p.pgLink AS pglink,
    p.reload_command AS reload_command
   FROM subjects s1
     RIGHT JOIN certificates c ON s1.certificate = c.id AND s1.isaltname = false
     LEFT JOIN subjects s2 ON s2.certificate = c.id AND s2.isaltname = true
     LEFT JOIN certificates_services cs ON c.id = cs.certificate
     LEFT JOIN services s ON cs.service = s.id
     LEFT JOIN targets t ON c.id = t.certificate
     LEFT JOIN disthosts d ON t.disthost = d.id
     LEFT JOIN jails j ON t.jail = j.id
     LEFT JOIN places p ON t.place = p.id
  WHERE s1.name = $1
  ORDER BY s1.name, s2.name, d.fqdn;
"""
q_instances = """
SELECT id
    FROM certinstances
    WHERE certificate = $1::INT
    ORDER BY id DESC;
"""

q_insert_cacert = """
    INSERT INTO Certificates(type)
        VALUES ($1)
        RETURNING id::int
"""

q_insert_cacert_subject = """
    INSERT INTO Subjects(type, name, isAltName, certificate)
        VALUES ($1, $2, FALSE, $3)
        RETURNING id::int
"""

q_update_authorized_until = """
    UPDATE Certificates
        SET authorized_until = $2::DATE
        WHERE id = $1
"""

q_fqdn_from_serial = """
SELECT s.name::TEXT
    FROM Subjects s, Certificates c, Certinstances i
    WHERE
        i.id = $1   AND
        i.certificate = c.id  AND
        s.certificate = c.id  AND
        NOT s.isaltname
"""

ps_all_cert_meta = None
ps_instances = None
ps_insert_cacert = None
ps_insert_cacert_subject = None
ps_update_authorized_until = None
ps_fqdn_from_serial = None


# ------------- public functions --------------


# ------------------------ Some string checking classes -------------------------
# From: https://stackoverflow.com/questions/7255655/how-to-subclass-str-in-python?answertab=votes#tab-top


class EncAlgo(str):
    def __new__(cls, content):
        assert content in ('rsa', 'ec', 'rsa plus ec')
        return str.__new__(cls, content)


class EncAlgoCKS(str):
    def __new__(cls, content):
        assert content in ('rsa', 'ec')
        return str.__new__(cls, content)


class SubjectType(str):
    def __new__(cls, content):
        assert content in ('CA', 'client', 'server', 'reserved')
        return str.__new__(cls, content)


class CertType(str):
    def __new__(cls, content):
        assert content in ('LE', 'local')
        return str.__new__(cls, content)


class CertState(str):
    def __new__(cls, content):
        assert content in ('reserved', 'issued', 'prepublished', 'deployed', 'revoked', 'expired', 'archived')
        return str.__new__(cls, content)


class PlaceCertFileType(str):
    def __new__(cls, content):
        assert content in ('cert only', 'separate', 'combine key', 'combine cacert', 'combine both')
        return str.__new__(cls, content)


# --------------- public class Certificate --------------

_all_CMs = {}

def CM(db: db_conn, name: str) -> 'Certificate':
    """
    Obtain a Certificate (cert meta) instance
    :param db: opened DB connection
    :param name: cert name
    :return:
    """
    if name in _all_CMs:
        return _all_CMs[name]
    return Certificate(db, name)

class Certificate(object):
    """
    Certificate meta data class.
    In-memory representation of DB backed meta information.
    """

    @staticmethod
    def fqdn_from_instance_serial(db: db_conn, serial: int):
        """
        Obtain cert subject name from instance serial

        @param db:      Opened database handle
        @type db:
        @param serial:  instance serial
        @type serial:   integer
        @rtype:         name as string
        @exceptions:
        """

        global ps_fqdn_from_serial

        if not ps_fqdn_from_serial:
            ps_fqdn_from_serial = db.prepare(q_fqdn_from_serial)

        result = ps_fqdn_from_serial.first(serial)
        sld('Certificate.fqdn_from_instance_serial found fqdn={} from row_id={}'.format(result, serial))
        if result: return result
        sle('No cert meta found for serial {}.'.format(serial))
        sys.exit(1)

    @staticmethod
    def ca_cert_meta(db: db_conn, name: str, cert_type: Optional[CertType]=None) -> Optional['Certificate']:
        """
        Return cert meta instance by subject name,
        inserting rows in certificates, subjects and certinstances if necessary
        :param db:          opened database connection
        :param name:        subject name of certificate
        :param cert_type:   Optional CertType. Must be set, if New CA cert to be created in case none exists in DB
        :return:            Certificate instance
        """

        global ps_insert_cacert, ps_insert_cacert_subject

        cm = CM(db, name)
        if cm.row_id:                             # do we have a row in db?
            return cm  # yes, return existing meta instance
        assert cert_type, '?Missing cert_type for of new CA CM'
        sln('Inserting CA cert meta {}, cert type {} into DB'.format(name, cert_type))
        if not ps_insert_cacert:
            ps_insert_cacert = db.prepare(q_insert_cacert)
        (certificates_row_id,) = ps_insert_cacert.first(cert_type)
        if not certificates_row_id:
            raise AssertionError('CA_cert_meta: ps_insert_cacert failed')
        if not ps_insert_cacert_subject:
            ps_insert_cacert_subject = db.prepare(q_insert_cacert_subject)
        (subjects_row_id,) = ps_insert_cacert_subject.first('CA', name, certificates_row_id)
        if not subjects_row_id:
            raise AssertionError('CA_cert_meta: ps_insert_cacert_subject failed')
        cm = CM(db, name)
        if cm.row_id and cm.cert_type == cert_type:
            return cm
        sle('Inserting of CA cert meta {} into DB failed'.format(name))

    @staticmethod
    def names(db: db_conn) -> [list]:
        """
        Obtain list of cert names
        :param db:  opened database connection
        :return:    list of all cert names in db
        """
        names = []
        row_list = db.query("""
            SELECT name from Subjects
                WHERE isAltName = FALSE
                ORDER BY name""", )
        for (name,) in row_list:
            names.append(name)
        return names

    @staticmethod
    def random_ci(db: db_conn) -> 'CertInstance':
        """
        Obtain a random cert instance as temporary cacert_ci while creating 1st cert instance
        :param db:   opened database connection
        :return: CertInstance instance
        """
        for cm in _all_CMs.values():
            if cm.cert_instances:
                return cm.cert_instances[0]
        # no cm with ci's loaded - look at DB
        for name in Certificate.names(db):
            cm = CM(db, name)
            if cm.cert_instances:
                return cm.cert_instances[0]
        sle('Can''t find any CertInstance in DB. Can''t store new CA cert. Can''t continue.')
        sys.exit(1)


    def __del__(self):
        global _all_CMs
        if not _all_CMs:
            return
        if self.name in _all_CMs:
            del _all_CMs[self.name]

    def __init__(self, db: db_conn, name: str):
        """
        Create or load a certificate meta instance.
        :param db: opened database connection
        :param name: subject name of certificate, ignored, if serial present
        :param serial: row_id of instance, whose cert meta we are creating  # FIXME # ???
        """

        global ps_all_cert_meta, ps_instances, _all_CMs

        if name in _all_CMs:
            raise AssertionError('Attempt to instantiate Certificate instance {} twice'.format(name))

        self.db = db
        self.name = name
        _all_CMs[name] = self

        self.altnames = []
        self.tlsaprefixes = {}
        self.disthosts = {}

        self.row_id = None

        with self.db.xact(isolation='SERIALIZABLE', mode='READ ONLY'):
            if not ps_all_cert_meta:
                ps_all_cert_meta = db.prepare(q_all_cert_meta)
            for row in ps_all_cert_meta(self.name):
                if not self.row_id:
                    self.row_id = row['c_id']
                    self.cert_type = CertType(row['c_type'])
                    self.disabled = row['c_disabled']
                    self.authorized_until = row['authorized_until']
                    self.subject_type = SubjectType(row['subject_type'])
                    self.encryption_algo = EncAlgo(row['encryption_algo'])
                    self.ocsp_must_staple = row['ocsp_must_staple']
                    sld('----------- {}\t{}\t{}\t{}\t{}\t{}\t{}'.format(
                        self.row_id,
                        self.name,
                        self.cert_type,
                        self.disabled,
                        self.authorized_until,
                        self.subject_type,
                        self.encryption_algo,
                        self.ocsp_must_staple)
                    )
                if row['alt_name']: self.altnames.append(row['alt_name'])
                if row['tlsaprefix']: self.tlsaprefixes[row['tlsaprefix']] = 1

                # crate a tree from rows:  dh1... -> jl1... -> pl1..., )

                if row['dist_host']:
                    if row['dist_host'] in self.disthosts:
                        dh = self.disthosts[row['dist_host']]
                    else:
                        dh = {'jails': {}}
                        self.disthosts[row['dist_host']] = dh
                        jr = ''
                        if row['jailroot']: jr = row['jailroot']
                        self.disthosts[row['dist_host']]['jailroot'] = jr

                    if row['jail']:
                        if row['jail'] == '':
                            raise Exception('Empty jail name of disthost {} in DB - '
                                            'Jail names must not be empty'.format(row['dist_host']))
                        jail_name = row['jail']
                    else:
                        jail_name = ''

                    if jail_name in dh['jails']:
                        jl = dh['jails'][jail_name]
                    else:
                        jl = {'places': {}}
                        dh['jails'][jail_name] = jl

                    if row['place']:
                        if row['place'] not in jl['places']:
                            p = Place(
                                name=row['place'],
                                cert_file_type=row['cert_file_type'],
                                cert_path=row['cert_path'],
                                key_path=row['key_path'],
                                uid=row['uid'],
                                gid=row['gid'],
                                mode=row['mode'],
                                chownboth=row['chownboth'],
                                pglink=row['pglink'],
                                reload_command=row['reload_command']
                            )
                            jl['places'][row['place']] = p
                    else:
                        sln('Missing Place in Disthost {}'.format(row['dist_host']))

                sld('altname:{}\tdisthost:{}\tjail:{}\tplace:{}'.format(
                    row['alt_name'] if row['alt_name'] else '',
                    row['dist_host'] if row['dist_host'] else '',
                    row['jail'] if row['jail'] else '',
                    row['place'] if row['place'] else '')
                )
        sld('tlsaprefixes of {}: {}'.format(self.name, self.tlsaprefixes))

        if not ps_instances:
            ps_instances = db.prepare(q_instances)

        self.cert_instances = []
        for row in ps_instances(self.row_id):
            ci = CertInstance(row_id=row['id'], cert_meta=self)
            self.cert_instances.append(ci)

        # return super.__init__(cls)

    def create_instance(self,
                        state: Optional[CertState],
                        not_before: datetime.datetime,
                        not_after: datetime.datetime,
                        ca_cert_ci: Optional['CertInstance']=None,
                        ocsp_ms: Optional[bool] = True,
                        cert_key_stores: Optional[dict]={}
                        ) -> 'CertInstance':
        assert ca_cert_ci or self.subject_type == SubjectType('CA'), '?CM.create_instance called wthout ca_cert_ci of none-CA CM'
        the_state = CertState(state) if state else CertState('reserved')
        the_ocsp_ms = ocsp_ms if ocsp_ms else self.ocsp_must_staple

        ci = CertInstance(cert_meta=self,
                          state=the_state,
                          ocsp_ms=the_ocsp_ms,
                          not_before=not_before,
                          not_after=not_after,
                          ca_cert_ci=ca_cert_ci,
                          cert_key_stores=cert_key_stores)
        ci._save()                             # obtain a row_id to make it unique
        for a_ci in self.cert_instances:
            assert ci.row_id != a_ci.row_id, '?Duplicate CI found with row_id={} and cert meta={}'.format(
                ci.row_id, self.name)
        assert ci not in self.cert_instances, '?Attempted to create CI a 2nd time with row_id={} and cert meta={}'.format(
                ci.row_id, self.name)
        self.cert_instances.append(ci)
        return ci

    def save_instance(self, ci: 'CertInstance'):
        """
        Save a new instance of CertInstance in DB backend and store it in self.cert_instances
        :param  ci  the CertInstance instance to save
        :return:
        """
        if ci._save():  # _ci._save() is only for usage by Certificate
            assert ci in self.cert_instances, ('?Attempt to save CI which was not created by CM.create_instance'
                                              'with row_id={} and cert meta={}'.format(
                                                    ci.row_id, self.name))

    def delete_instance(self, ci: 'CertInstance') -> int:
        """
        Delete an instance of CertInstance and its DB backup
        :param ci: The instance to delete
        :return:
        """
        assert ci in self.cert_instances, '?Attempt to delete CI which was not created by CM.create_instance'
        'with row_id={} and cert meta={}'.format(
            ci.row_id, self.name)
        result = ci._delete()
        if ci in self.cert_instances:
            self.cert_instances.remove(ci)
        return result

    @property
    def in_db(self):
        """
        Returns true if the cert meta has rows in the db.
        Returns None, if this cert meta has not yet been saved in the db.
        :return: bool
        """
        if self.row_id:
            return True

    @property
    def most_recent_instance(self) -> 'CertInstance':
        return self.cert_instances[-1]

    @property
    def most_recent_active_instance(self):

        return sorted(self.cert_instances, key=lambda ci: ci.row_id)[-1]

    @property
    def active_instances(self) -> dict:
        """
        Return dict with active instances as values.
        Active means: Valid today.
        :return: dict with state as key and ci as value
         """
        ret_dict = {}

        for ci in (self.cert_instances):
            if ci.active:
                ret_dict[ci.state] = ci

        return ret_dict

    def instance_from_row_id(self, row_id: int) -> Optional['CertInstance']:
        """
        Obtain the instance by DB row_id
        :param row_id:
        :return: the CertInstance of an issued certificate
        :exception: raises AssertionError if CI not found
        """
        for ci in (self.cert_instances):
            if ci.row_id == row_id:
                return ci

    def zone_and_FQDN_from_altnames(self) -> List[Optional[Tuple[str, str]]]:
        """
        Retrieve zone and FQDN of TLSA RRs.
        :return: List of tuples, each containing 2 strings: zone name and fqdn of TLSA RR
        """
        retval = []
        alt_names = [self.name, ]
        if len(self.altnames) > 0:
            alt_names.extend(self.altnames)

        for fqdn in alt_names:
            fqdn_tags = fqdn.split(sep='.')
            for i in range(1, len(fqdn_tags) + 1):
                zone = '.'.join(fqdn_tags[-i::])
                if (Path(Pathes.zone_file_root) / zone).exists():
                    sld('{}'.format(str(Path(Pathes.zone_file_root) / zone)))
                    retval.append((zone, fqdn))
                    break
        return retval

    def TLSA_hashes(self, cert_instance: Optional['CertInstance']) -> Optional[Dict[EncAlgoCKS, str]]:
        """
        Return TLSA hashes of instance, which is valid today and in prepublish state ##FIXME## prepublish state??
        :param cert_instance: optional CertInstance, whose hashes to be returned.
                                If None, hash of most recent active CertInstance is returned.
                                ##FIXME## must returned CI in prepublish state??
        :return: Dict with algo as key and hash as value
        """
        ci = None
        if cert_instance:
            assert cert_instance in self.cert_instances
            ci = cert_instance
        else:
            for ci in reversed(self.cert_instances):
                if ci.active:
                    break
        if not ci or not ci.active:
            sle('Certificate.TLSA_hashes found no active instance for {} and cert_instance {}'.
                format(self.name, cert_instance))
            return

        d = {}
        for k in ci.cksd.keys():
            d[ci.cksd[k].algo] = ci.cksd[k].hash
            sld('Certificate.TLSA_hashes: Cert: {} Algo: {} and Hash: {}'.
                format(self.name, ci.cksd[k].algo, ci.cksd[k].hash))

        return d

    def cacert_PEM(self, ci: 'CertInstance') -> str:
        """
        Return the PEM encoded CA cert of an CertInstance
        :param ci: CertInstance of which to return its CA cert
        :return: PEM encoded CA cert
        """
        cksd = ci.ca_cert_ci.cksd
        if len(cksd) != 1:
            sle('Not exactly one CertKeyStore for CA cert of {}, CA cert ci.ca_cert_ci.row_id={} - giving up.'.format(
                self.name, ci.ca_cert_ci.row_id))
            sys.exit(1)
        for k in cksd.keys():
            return cksd[k].cert


    def update_authorized_until(self, until: Optional[datetime.datetime]):
        """
        Update authorized_until of Certificate meta instance
        :param until:
        :return:
        """
        global ps_update_authorized_until

        self.authorized_until = until

        # resetting of authorized_until allowd only by local certs
        assert until or self.cert_type == 'local', \
            'update_authorized_until {} called for {}'.format(until, self.name)

        if not ps_update_authorized_until:
            ps_update_authorized_until = self.db.prepare(q_update_authorized_until)

        updates = ps_update_authorized_until.first(
            self.row_id,
            until
        )
        return updates


# --------------- class Place --------------

class Place(object):
    """
    Place is a collection of certificate metadata, describing details of
    deployment place. It is unique per service or server software.
    It may be re-used at multiple target hosts.
    Backed up in DB table Places'
    """

    def __init__(self, name: str = None,
                 cert_file_type=None,
                 cert_path=None,
                 key_path=None,
                 uid=None,
                 gid=None,
                 mode=None,
                 chownboth=None,
                 pglink=None,
                 reload_command=None):
        """

        :param name: Name of Place
        :param cert_file_type:
        :param cert_path:
        :param key_path:
        :param uid:
        :param gid:
        :param mode:
        :param chownboth:
        :param pglink:
        :param reload_command:
        """
        self.name = name
        self.cert_file_type = cert_file_type
        self.cert_path = cert_path
        self.key_path = key_path
        self.uid = uid
        self.gid = gid
        self.mode = mode
        self.chownBoth = chownboth
        self.pgLink = pglink
        self.reload_command = reload_command


# part module for classes CertInstance and CertKeyStore

# ---------------  prepared SQL queries for class CertInstance  --------------

q_load_instance = """
    SELECT ci.id, ci.certificate AS cm_id, ci.state::TEXT, ci.ocsp_must_staple, ci.not_before, ci.not_after,
                        ci.CAcert AS ca_cert_ci_id, d.id AS ckd_id, d.encryption_algo::TEXT, d.cert, d.key, d.hash
            FROM CertInstances ci
            LEFT JOIN CertInstances ca ON ci.CAcert = ca.id
            LEFT JOIN CertKeyData d    ON d.certinstance = ci.id
            WHERE
                ci.id = $1::INT;
"""

q_delete_instance = """
    DELETE FROM Certinstances
        WHERE id = $1
"""

q_store_instance = """
    INSERT INTO CertInstances
            (certificate, state, ocsp_must_staple, not_before, not_after, cacert)
        VALUES ($1::INTEGER, $2, $3::BOOLEAN, $4::DATE, $5::DATE, $6::INTEGER)
        RETURNING id::int
"""

q_store_cacert_instance = """
    INSERT INTO CertInstances
            (certificate, state, ocsp_must_staple, not_before, not_after, cacert)
        VALUES ($1::INTEGER, $2, $3::BOOLEAN, $4::DATE, $5::DATE, currval('certinstances_id_seq'))
        RETURNING id::int
"""

q_update_instance = """
    UPDATE CertInstances
        SET certificate=$1::INTEGER, state=$2, ocsp_must_staple=$3::BOOLEAN, 
            not_before=$4::DATE, not_after=$5::DATE, cacert=$6::INTEGER
        WHERE id=$7::INTEGER
"""
ps_load_instance = None
ps_delete_instance = None
ps_store_instance = None
ps_store_cacert_instance = None
ps_update_instance = None


# ---------------------------- class CertInstance (CI)---------------------------

@total_ordering
class CertInstance(object):
    """
    Issued certificate instance class.
    In-memory representation of DB backend CertInstances.
    """

    def __init__(self,
                 cert_meta: Certificate,
                 row_id: int = None,
                 state: CertState = None,
                 ocsp_ms: bool = None,
                 not_before: datetime.datetime = None,
                 not_after: datetime.datetime = None,
                 ca_cert_ci: Optional['CertInstance'] = None,
                 cert_key_stores: Dict[EncAlgoCKS, 'CertKeyStore'] = {}):
        """
        Load or create a certificate instance (CI), which may be incomplete and may be updated later
        :param cert_meta: Our Certificate meta instance (required)
        :param row_id: id of row in DB, if supplied, other args may be empty
        :param state: State of new instance
        :param ocsp_ms:
        :param not_before:
        :param not_after:
        :param ca_cert_ci:  Must be supplied, if row_id is empty and cert meta is not a CA
        :param cert_key_stores:
        """

        global ps_load_instance

        if not cert_meta:
            raise AssertionError('CertInstance: Argument cert_meta missing')
        self.cm = cert_meta
        if not ps_load_instance:
            ps_load_instance = self.cm.db.prepare(q_load_instance)

        self.state = CertState(state) if state else CertState('reserved')
        self.ocsp_ms = ocsp_ms if ocsp_ms else cert_meta.ocsp_must_staple
        self.not_before = not_before
        self.not_after = not_after
        self.ca_cert_ci = ca_cert_ci
        assert ca_cert_ci or row_id or cert_meta.subject_type == SubjectType('CA')
        self.cksd = cert_key_stores if cert_key_stores else {}
        if row_id:
            self.row_id = row_id
            rows = ps_load_instance(self.row_id)
            if not rows:
                raise AssertionError('CertInstance: row_id {} does not exist'.format(self.row_id))

            self.cksd = {}
            first = True
            for row in rows:
                if first:
                    self.state = row['state']
                    self.ocsp_ms = row['ocsp_must_staple']
                    self.not_before = row['not_before']
                    self.not_after = row['not_after']
                    if self.cm.subject_type == SubjectType('CA'):       # are we a CA ?
                        self.ca_cert_ci = self                          # yes - we are issued from our self
                    else:
                        ca_cert_fqdn = Certificate.fqdn_from_instance_serial(cert_meta.db, row['ca_cert_ci_id'])
                        ca_cert_meta = CM(cert_meta.db, ca_cert_fqdn)   # This have been loaded already by operate.execute_from_command_line
                        self.ca_cert_ci = ca_cert_meta.instance_from_row_id(row['ca_cert_ci_id'])
                        assert self.ca_cert_ci, '? No CI for CA cert found, while loading CI of {}:{}'.format(cert_meta.name, self.row_id)
                    sld('Loaded CertInstance row_id={}, state={}, ocsp_ms={}, not_before={}, not_after={}, ca_cert_ci_id={}'
                        .format(self.row_id, self.state, self.ocsp_ms,
                                self.not_before.isoformat(), self.not_after.isoformat(), row['ca_cert_ci_id']))
                    first = False
                if not row['ckd_id']:
                    break                                       # we have no certkeydata
                cks = CertKeyStore(row_id=row['ckd_id'],
                               cert_instance=self,
                               algo=EncAlgoCKS(row['encryption_algo']),
                               cert=row['cert'],
                               key=row['key'],
                               hash=row['hash'])
                self.cksd[EncAlgoCKS(row['encryption_algo'])] = cks
                sld('Loaded CertKeyStore with row_id={}, Algo={}, Hash={}'.format(row['id'], row['encryption_algo'], row['hash']))

        else:
            self.row_id = None
            if not self.ca_cert_ci:
                self.ca_cert_ci = self


    def __str__(self):
        return str(self.row_id if self.row_id else self.cm.name + 'instance')

    def __eq__(self, other):
        return self.row_id == other.row_id

    def __lt__(self, other):
        return self.row_id < other.row_id

    def __hash__(self):
        return self.row_id

    def _delete(self) -> int:
        """
        Delete this instance of CertInstance in DB backend and all its CertKeyStores (per cascaded delete)
        :return:    Number of rows deleted
        """
        global ps_delete_instance

        if not ps_delete_instance:
            ps_delete_instance = self.cm.db.prepare(q_delete_instance)
        sld('CI._delete called for row_id {}'.format(self.row_id))
        if self.row_id:
            result = ps_delete_instance.first(self.row_id)
            sld('CI._delete returned {} for row_id {}'.format(result, self.row_id))
            return result
        else:
            return 0

    def _save(self):
        """
        Store this instance of CertInstance in DB backend (must not exist in DB)
        :return:
        """
        global ps_store_instance, ps_update_instance

        if not ps_store_instance:
            ps_store_instance = self.cm.db.prepare(q_store_instance)
        if not ps_update_instance:
            ps_update_instance = self.cm.db.prepare(q_update_instance)
        sld ('CertInstance._save(): cm.row_id={}, state={}, ocsp_ms={}, not_before={}, not_after={}, ca_cert_ci.row_i={}'.format(
            self.cm.row_id,
            self.state,
            self.ocsp_ms,
            self.not_before,
            self.not_after,
            self.ca_cert_ci.row_id
        ))
        if self.row_id:
            result = ps_update_instance.first(self.cm.row_id,
                                        self.state,
                                        self.ocsp_ms,
                                        self.not_before,
                                        self.not_after,
                                        self.ca_cert_ci.row_id,
                                        self.row_id)
            assert result==1,'?Failed to update CI with row_id {} of cert {}'.format(self.row_id, self.cm.name)
        else:
            if self.ca_cert_ci == self:     # we are a CI of a CA cert meta
                if not ps_store_cacert_instance:
                    ps_store_cacert_instance = self.cm.db.prepare(q_store_cacert_instance)
                self.row_id = ps_store_cacert_instance.first(self.cm.row_id,
                                                             self.state,
                                                             self.ocsp_ms,
                                                             self.not_before,
                                                             self.not_after)
            else:
                self.row_id = ps_store_instance.first(self.cm.row_id,
                                                      self.state,
                                                      self.ocsp_ms,
                                                      self.not_before,
                                                      self.not_after,
                                                      self.ca_cert_ci.row_id)
            assert self.row_id, '?Failed to INSERT CI of {}'.format(self.cm.name)
        sld('CertInstance._save(): Returned row_id={}'.format(self.row_id))

    def store_cert_key(self,
                       algo: EncAlgoCKS,
                       cert: x509.Certificate,
                       key: bytes) -> 'CertKeyStore':
        """
        Store a new certificate in a CertKeyStore instance and in the backend
        :param algo:    encryption algorythm
        :param cert:    certificate, cryptography.x509.Certificate instance
        :param key:     privat key of cert, raw format
        :return:        new instance of CertKeyStore
        """
        the_algo = EncAlgoCKS(algo) if algo else EncAlgoCKS('rsa')
        if the_algo in self.cksd:
            AssertionError('CertInstance: store_cert_key Attempt to overwrite cert for {} with algo {}'.
                           format(self.cm.name, the_algo))
        if not self.not_before:
            self.not_before = cert.not_valid_before
        if not self.not_after:
            self.not_after = cert.not_valid_after

        if not self.row_id:                                 # CertKeyStore need our row_id
            self._save()

        cks = CertKeyStore(
            row_id=None,
            cert_instance=self,
            algo=the_algo,
            cert=cert,
            key=key)
        self.cksd[the_algo] = cks
        return cks

    @property
    def active(self) -> bool:
        """
        Return True is this CertKeyStore's certificate is valid today
        :return: bool
        """
        if (self.not_before < datetime.datetime.now() and
                self.not_after > datetime.datetime.now()):
            return True
        else:
            return False

    @property
    def the_cert_key_stores(self) -> Dict[EncAlgoCKS, 'CertKeyStore']:
        """
        Return the dict of CertKeyStores
        :return: dict with EncAlgo as key and CertKeyStore as value
        """
        return self.cksd


# ---------------  prepared SQL queries for class CertKeyStore  --------------


q_store_certkeydata = """
    INSERT INTO CertKeyData
            (certinstance, encryption_algo, cert, key, hash, created)
        VALUES ($1::INTEGER, $2, $3, $4, $5, now())
        RETURNING id::int
"""
q_update_certkeydata = """
    UPDATE CertKeyData
        SET
            certinstance = $2::INT,
            encryption_algo = $3,
            cert = $4,
            key = $5,
            hash = $6
        WHERE id = $1::INT;
"""

q_hash = """
    SELECT hash
        FROM CertInstances
        WHERE
            id = $1
"""
ps_store_certkeydata = None
ps_update_certkeydata = None
ps_hash = None


# ---------------------------- class CertKeyStore (CKS) ---------------------------

class CertKeyStore(object):
    """
    Cert key data store class class.
    In-memory representation of DB backend CertKeyData.
    """

    _cert_key_stores = {}  # ensures that we have only one cert key store per hash

    @staticmethod
    def hash_from_cert(cert: Union[x509.Certificate, bytes]) -> str:
        """
        return TLSA suitable hash from cryptography.x509.Certificate instance
        :cert:  certificate to compute hash from
        :return: the hash as str
        """
        if isinstance(cert, bytes):
            cert = x509.load_pem_x509_certificate(cert, default_backend())
        elif not isinstance(cert, x509.Certificate):
            raise AssertionError('CertKeyStore.hash_from_cert called with unexpected cert object type')
        return binascii.hexlify(
            cert.fingerprint(SHA256())).decode('ascii').upper()

    @staticmethod
    def certinstance_from_cert(cert: x509.Certificate) -> Optional[CertInstance]:
        """
        return the CertInstance from a (loaded) cert
        :return: CertInstance or None
        """
        hash = CertKeyStore.hash_from_cert(cert)
        if hash in CertKeyStore._cert_key_stores:
            return CertKeyStore._cert_key_stores[hash].ci
        else:
            return None

    @staticmethod
    def ci_from_cert_and_name(db: db_conn, cert: x509.Certificate, name: str) -> Optional[CertInstance]:
        """
        Return CertInstance of a given cert and a cert meta name
        :param db: opened database connection
        :param cert: x509.Certificate istance
        :param name: cert met name
        :return: CertInstance or None
        """

        hash = CertKeyStore.hash_from_cert(cert)
        cm = CM(db=db, name=name)
        if not cm.row_id:           # make shure cert meta has been loaded (with all dependant  ci,cks)
            return None             # No cert meta with that name
        if hash in CertKeyStore._cert_key_stores:
            return CertKeyStore._cert_key_stores[hash].ci
        else:
            return None

    def __init__(self,
                 row_id: Optional[int],
                 cert_instance: CertInstance,
                 algo: EncAlgoCKS,
                 cert: Union[x509.Certificate, bytes],
                 key: Optional[Union[RSAPrivateKeyWithSerialization, bytes]],
                 hash=None):
        """
        Create a new CertKeyStore instance
        :param row_id:          id in DB, cert and key are in DB storage format (key encrypted)
        :param cert_instance:   parent CertInstance, required
        :param algo:            cert encryption algo
        :param cert:            Certificate data, binary PEM (db storage) format assumed,
        :param key:             Key data, if row_id present,
                                (possibly, [if encryption in use]) encrypted binary PEM (db storage) format assumed,
                                else raw format
        :param hash:            sha256 hash of cert (TLSA hash format).
        """

        global ps_store_certkeydata

        if not cert_instance:
            raise AssertionError('CertKeyStore: Argument cert_instance missing')
        hash = CertKeyStore.hash_from_cert(cert)
        if hash in CertKeyStore._cert_key_stores:
            raise AssertionError('Attempt to create duplicate CertKeyStore for meta {}'.
                           format(CertKeyStore._cert_key_stores[hash].ci.cm.name))
        self.ci = cert_instance
        self.algo = EncAlgoCKS(algo) if algo else EncAlgoCKS('rsa')
        self.row_id = row_id
        sld('CertKeyStore.__init__: cm.name={}, algo={}, row_id={}'.format(cert_instance.cm.name, algo, row_id))
        if self.row_id:  # cert and key come from DB
            self._key = key  # self._key holds (encrypted) binary PEM format (=DB storage format)
            self._cert = cert  # self-_cert holds binary PEM format (=DB storage format)
            self.hash = hash
        else:  # new cert has been issued
            if not key or (not isinstance(key, RSAPrivateKey) and not isinstance(key, EllipticCurvePrivateKey)):
                AssertionError('CertKeyStore: Argument row_id is omitted and arument key'
                               'is not a RSAPrivateKey or EllipticCurvePrivateKey instance')
            if not cert or not isinstance(cert, x509.Certificate):
                AssertionError('CertKeyStore: Argument id is omitted and arument cert'
                               'is not a x509.Certificate instance')
            self._cert = cert.public_bytes(Encoding.PEM)
            self._key = self._encrypt_key(key) if key else b''
            self.hash = self.hash_from_cert(cert)

            self._save()

        CertKeyStore._cert_key_stores[hash] = self

    def __del__(self):
        if self.hash in CertKeyStore._cert_key_stores:
            del CertKeyStore._cert_key_stores[self.hash]
        if self.algo in self.ci.cksd:
            del self.ci.cksd[self.algo]

    @property
    def key(self) -> Optional[str]:
        """
        Return the decrypted key as PEM formated text
        :return: string or None if this CertKeyStore stores a CA cert
        """
        if self.ci.cm.cert_type == 'CA':
            return None
        else:
            clear_key = self._decrypt_key(self._key)
            return clear_key.decode('ascii')

    @property
    def key_for_ca(self) -> bytes:
        """
        Return the decrypted key as bytes
        :return: string or None if this CertKeyStore stores a CA cert
        """
        return self._key

    @property
    def cert(self) -> str:
        """
        Return the certificate as PEM formatted text
        :return: string
        """
        return self._cert.decode('ascii')

    @property
    def cert_for_ca(self) -> bytes:
        """
        Return the seralized cert as bytes
        :return:
        """
        return self._cert

    def _save(self) -> None:
        """
        Save this CertKeyStore instance in DB backend
        Creates a new row in certkeydata or updates an existing one (if self.row_id exists)
        :return:
        """
        global ps_store_certkeydata, ps_update_certkeydata

        sld('CertKeyStore._save(): cm.name={}, row_id={}, ci.row_id={}, algo={}, hash={}'.format(
            self.ci.cm.name, self.row_id, self.ci.row_id, self.algo, self.hash))
        if self.row_id:
            if not ps_update_certkeydata:
                ps_update_certkeydata = self.ci.cm.db.prepare(q_update_certkeydata)
            updates = ps_update_certkeydata(
                self.row_id,
                self.ci.row_id,
                self.algo,
                self._cert,
                self._key,
                self.hash
            )
            if updates != 1:
                raise DBStoreException('?Failed to update CertKeyStore in DB')
        else:
            if not ps_store_certkeydata:
                ps_store_certkeydata = self.ci.cm.db.prepare(q_store_certkeydata)
            (self.row_id) = ps_store_certkeydata.first(
                self.ci.row_id,
                self.algo,
                self._cert,
                self._key,
                self.hash
            )
            if not self.row_id and not isinstance(self.row_id, int):
                raise DBStoreException('?Failed to store CertKeyStore in DB: returned id={}'.format(self.row_id))

    def _key_to_PEM(self, key: RSAPrivateKeyWithSerialization) -> bytes:
        """
        Serialize a key to PEM format
        :param key: Unencrypted binary key
        :return:    key in PEM format as bytes
        """
        return key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=NoEncryption())

    def _encrypt_key(self, the_binary_cert_key) -> bytes:
        """
        Serialize and encrypt a private key to PEM format
        :param the_binary_cert_key: Unencrypted binary key
        :return:                    key in PEM format as bytes
        """
        global db_encryption_key, db_encryption_in_use

        if not db_encryption_in_use:
            return self._key_to_PEM(the_binary_cert_key)
        else:
            encryption_type = BestAvailableEncryption(db_encryption_key)
            key_pem = the_binary_cert_key.private_bytes(
                Encoding.PEM,
                PrivateFormat.TraditionalOpenSSL,
                encryption_type)
        return key_pem

    def _decrypt_key(self, encrypted_key_bytes) -> bytes:
        """
        Load and decrypt a private key
        :param encrypted_key_bytes: encrypted key in binary PEM format
        :return: key as bytes
        """

        if not db_encryption_in_use:
            return encrypted_key_bytes
        else:
            decrypted_key = load_pem_private_key(
                encrypted_key_bytes,
                password=db_encryption_key,
                backend=default_backend())
            key_pem = decrypted_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
        return key_pem
