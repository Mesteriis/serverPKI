# -*- coding: utf-8 -*-

"""
 Copyright (c) 2006-2014 Axel Rau, axel.rau@chaos1.de
 All rights reserved.

 Redistribution and use in source and binary forms, with or without
 modification, are permitted provided that the following conditions
 are met:

    - Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
    - Redistributions in binary form must reproduce the above
      copyright notice, this list of conditions and the following
      disclaimer in the documentation and/or other materials provided
      with the distribution.

 THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
 FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
 COPYRIGHT HOLDERS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
 INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
 BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
 LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
 CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
 ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 POSSIBILITY OF SUCH DAMAGE.

"""

# serverpki Certificate class module
# requires python 3.4.

#--------------- imported modules --------------
import binascii
import datetime
from hashlib import sha256
import logging
from pathlib import Path
import os
import sys
import time

import iso8601
from cryptography.hazmat.primitives.hashes import SHA256

from manuale.acme import Acme
from manuale import crypto as manuale_crypto
from manuale import issue as manuale_issue
from manuale import cli as manuale_cli
from manuale import errors as manuale_errors

#--------------- local imports --------------
from pki.cacert import create_CAcert_meta
from pki.config import Pathes, X509atts, LE_SERVER, SUBJECT_LE_CA
from pki.utils import sld, sli, sln, sle, options, update_certinstance
from pki.utils import zone_and_FQDN_from_altnames, updateSOAofUpdatedZones
from pki.utils import reloadNameServer, updateZoneCache

# --------------- manuale logging ----------------

logger = logging.getLogger(__name__)

#--------------- Places --------------
places = {}

#--------------- classes --------------

class DBStoreException(Exception):
    pass

class KeyCertException(Exception):
    pass

#---------------  prepared SQL queries for create_LE_instance  --------------

q_insert_LE_instance = """
    INSERT INTO CertInstances 
            (certificate, state, cert, key, hash, cacert, not_before, not_after)
        VALUES ($1::INTEGER, 'issued', $2, $3, $4, $5, $6::TIMESTAMP, $7::TIMESTAMP)
        RETURNING id::int
"""
ps_insert_LE_instance = None

        
#--------------- public functions --------------

        
def issue_LE_cert(cert_meta):

    global ps_insert_LE_instance

    # Set up logging
    root = logging.getLogger('manuale')
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)

    alt_names = [cert_meta.name, ]
    if len(cert_meta.altnames) > 0:
        alt_names.extend(cert_meta.altnames)

    os.chdir(str(Pathes.work))
    try:
        account = manuale_cli.load_account(str(Pathes.le_account))
    except:
        sle('Problem with Lets Encrypt account data at {}'.format(
                                                str(Pathes.le_account)))
        exit(1)

    if not (cert_meta.authorized_until and
                    cert_meta.authorized_until >= datetime.datetime.now()):
        if not _authorize(cert_meta, account):
            return False
    
    sli('Creating key (%d bits) and cert for %s %s' %
        (int(X509atts.bits), cert_meta.subject_type, cert_meta.name))
    certificate_key = manuale_crypto.generate_rsa_key(X509atts.bits)
    csr = manuale_crypto.create_csr(certificate_key, alt_names)
    acme = Acme(LE_SERVER, account)
    try:
        sli('Requesting certificate issuance from LE...')
        result = acme.issue_certificate(csr)
    except IOError as e:
        sle("Connection or service request failed. Aborting.")
        raise manuale_errors.ManualeError(e)
    try:
        certificate = manuale_crypto.load_der_certificate(result.certificate)
    except IOError as e:
        sle("Failed to load new certificate. Aborting.")
        raise manuale_errors.ManualeError(e)

    if result.intermediate:
        intcert = manuale_crypto.load_der_certificate(result.intermediate)
        intcert_instance_id = _get_intermediate_instance(cert_meta.db, intcert)
    else:
        sle('Missing intermediate cert. Can''t store in DB')
        exit(1)

    not_valid_before = certificate.not_valid_before
    not_valid_after = certificate.not_valid_after

    cert_pem = manuale_crypto.export_pem_certificate(certificate)
    key_pem = manuale_crypto.export_rsa_key(certificate_key)
    tlsa_hash = binascii.hexlify(
        certificate.fingerprint(SHA256())).decode('ascii').upper()

    sli('Certificate issued. Valid until {}'.format(not_valid_after.isoformat()))
    sli('Hash is: {}'.format(tlsa_hash))


    if not ps_insert_LE_instance:
        ps_insert_LE_instance = cert_meta.db.prepare(q_insert_LE_instance)
    (instance_id) = ps_insert_LE_instance.first(
            cert_meta.cert_id,
            cert_pem,
            key_pem,
            tlsa_hash,
            intcert_instance_id,
            not_valid_before,
            not_valid_after)
    if instance_id:
        return True
    sle('Failed to store new cert in DB backend')
    
    
#---------------  prepared SQL queries for private functions  --------------

q_query_LE_intermediate = """
    SELECT id from CertInstances
        WHERE hash = $1 
"""
ps_query_LE_intermediate = None

        
#--------------- private functions --------------

def _get_intermediate_instance(db, int_cert):

    global ps_query_LE_intermediate
    
    hash = binascii.hexlify(
        int_cert.fingerprint(SHA256())).decode('ascii').upper()
    
    if not ps_query_LE_intermediate:
        ps_query_LE_intermediate = db.prepare(q_query_LE_intermediate)
    (instance_id) = ps_query_LE_intermediate.first(hash)
    if instance_id:
        return instance_id
    
    # new intermediate - put it into DB
    
    instance_id = create_CAcert_meta(db, 'LE', SUBJECT_LE_CA)
    
    not_valid_before = int_cert.not_valid_before
    not_valid_after = int_cert.not_valid_after

    cert_pem = manuale_crypto.export_pem_certificate(int_cert)
    
    (updates) = update_certinstance(db, instance_id, cert_pem, b'', hash,
                                    not_valid_before, not_valid_after)
    if updates != 1:
        raise DBStoreException('?Failed to store intermediate certificate in DB')
    return instance_id

def _authorize(cert_meta, account):

    acme = Acme(LE_SERVER, account)
    thumbprint = manuale_crypto.generate_jwk_thumbprint(account.key)

    FQDNs = [cert_meta.name, ]
    if len(cert_meta.altnames) > 0:
        FQDNs.extend(cert_meta.altnames)

    try:
        # Get pending authorizations for each fqdn
        authz = {}
        for fqdn in FQDNs:
            sli("Requesting challenge for {}.".format(fqdn))
            created = acme.new_authorization(fqdn)
            auth = created.contents
            auth['uri'] = created.uri
            
            # Find the DNS challenge
            try:
                auth['challenge'] = [ch for ch in auth.get('challenges', []) if ch.get('type') == 'dns-01'][0]
            except IndexError:
                raise manuale_errors.ManualeError("Manuale only supports the dns-01 challenge. The server did not return one.")
            
            auth['key_authorization'] = "{}.{}".format(auth['challenge'].get('token'), thumbprint)
            digest = sha256()
            digest.update(auth['key_authorization'].encode('ascii'))
            auth['txt_record'] = manuale_crypto.jose_b64(digest.digest())
            
            authz[fqdn] = auth
        
        zones = {}
        
        sld('Calling zone_and_FQDN_from_altnames()')
        for (zone, fqdn) in zone_and_FQDN_from_altnames(cert_meta):
            if zone in zones:
                zones[zone].append(fqdn)
            else:
                zones[zone] = [fqdn]
        sld('zones: {}'.format(zones))
        # write one file with TXT RRS into related zone directory:
        for zone in zones.keys():
            dest = str(Pathes.zone_file_root / zone / Pathes.zone_file_include_name)
            lines = []
            for fqdn in zones[zone]:
                sld('fqdn: {}'.format(fqdn))
                auth = authz[fqdn]
                lines.append(str('_acme-challenge.{}.  IN TXT  \"{}\"\n'.format(fqdn, auth['txt_record'])))
            sli('Writing RRs: {}'.format(lines))
            with open(dest, 'w') as file:
                file.writelines(lines)
                ##os.chmod(file.fileno(), Pathes.zone_tlsa_inc_mode)
                ##os.chown(file.fileno(), pathes.zone_tlsa_inc_uid, pathes.zone_tlsa_inc_gid)
            updateZoneCache(zone)
        
        updateSOAofUpdatedZones()
        reloadNameServer()
        
        sli("{}: Waiting for DNS propagation. Checking in 10 seconds.".format(fqdn))
        time.sleep(10)
        
        # Verify each fqdn
        done, failed = set(), set()
        authorized_until = None
        
        for fqdn in FQDNs:
            sli('')
            auth = authz[fqdn]
            challenge = auth['challenge']
            acme.validate_authorization(challenge['uri'], 'dns-01', auth['key_authorization'])
    
            while True:
                sli("{}: waiting for verification. Checking in 5 seconds.".format(fqdn))
                time.sleep(5)
    
                response = acme.get_authorization(auth['uri'])
                status = response.get('status')
                if status == 'valid':
                    done.add(fqdn)
                    expires = response.get('expires', '(not provided)')
                    if not authorized_until:
                        authorized_until = iso8601.parse_date(expires)
                        sld('Authorization lasts until {}'.format(authorized_until))
                    sli("{}: OK! Authorization lasts until {}.".format(fqdn, expires))
                    break
                elif status != 'pending':
                    failed.add(fqdn)
    
                    # Failed, dig up details
                    error_type, error_reason = "unknown", "N/A"
                    try:
                        challenge = [ch for ch in response.get('challenges', []) if ch.get('type') == 'dns-01'][0]
                        error_type = challenge.get('error').get('type')
                        error_reason = challenge.get('error').get('detail')
                    except (ValueError, IndexError, AttributeError, TypeError):
                        pass
    
                    sle("{}: {} ({})".format(fqdn, error_reason, error_type))
                    break
    
        # remember new expiration date in DB
        updates = cert_meta.update_authorized_until(authorized_until)
        if updates != 1:
            sln('Failed to update DB with new authorized_until timestamp')
            
        # make include files empty
        for zone in zones.keys():
            dest = str(Pathes.zone_file_root / zone / Pathes.zone_file_include_name)
            with open(dest, 'w') as file:
                file.writelines(('', ))
                ##os.chmod(file.fileno(), Pathes.zone_tlsa_inc_mode)
                ##os.chown(file.fileno(), pathes.zone_tlsa_inc_uid, pathes.zone_tlsa_inc_gid)
            updateZoneCache(zone)
        updateSOAofUpdatedZones()
        reloadNameServer()
    
        if failed:
            sle("{} fqdn(s) authorized, {} failed.".format(len(done), len(failed)))
            sli("Authorized: {}".format(' '.join(done) or "N/A"))
            sle("Failed: {}".format(' '.join(failed)))
            return False
        else:
            sli("{} fqdn(s) authorized. Let's Encrypt!".format(len(done)))
            return True
        
    except IOError as e:
        sle('A connection or service error occurred. Aborting.')
        raise manuale_errors.ManualeError(e)
    