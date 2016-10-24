# -*- coding: utf-8 -*


"""
Certificate distribution module.
"""

import sys
from datetime import datetime
from io import StringIO
from pathlib import PurePath, Path
from os.path import expanduser
from os import chdir
from socket import timeout
import subprocess
from shutil import copy2
from time import sleep

from paramiko import SSHClient, HostKeys, AutoAddPolicy

from pki.config import Pathes, SSH_CLIENT_USER_NAME
from pki.utils import options as opts
from pki.utils import sld, sli, sln, sle
from pki.utils import updateZoneCache, zone_and_FQDN_from_altnames
from pki.utils import updateSOAofUpdatedZones, reloadNameServer
from pki.utils import update_state_of_instance

class MyException(Exception):
    pass

def deployCerts(certs, instance_id=None):

    """
    Deploy a list of (certificate. key and TLSA file, using sftp).
    Restart service at target host and reload nameserver.
    
    @param certs:       list of certificate meta data instances
    @type certs:        pki.cert.Certificate instance
    @param instance_id: optional id of specific instance
    @type instance_id:  int
    @rtype:             bool, false if error found
    @exceptions:
    Some exceptions (to be replaced by error messages and false return)
    """

    error_found = False
        
    limit_hosts = False
    only_host = []
    if opts.only_host: only_host = opts.only_host
    if len(only_host) > 0: limit_hosts = True
    
    skip_host = []
    if opts.skip_host: skip_host = opts.skip_host
    
    sld('limit_hosts={}, only_host={}, skip_host={}'.format(
                                            limit_hosts, only_host, skip_host))
    
    for cert in certs.values():
         
        if len(cert.disthosts) == 0: continue
        
        result = cert.instance(instance_id)
        if not result:
            sle('No valid cerificate for {} in DB - create it first'.format(
                                                                    cert.name))
            raise MyException('No valid cerificate for {} in DB - '
                                            'create it first'.format(cert.name))
        instance_id, cert_text, key_text, TLSA_text, cacert_text = result
        host_omitted = False
        
        for fqdn,dh in cert.disthosts.items():
        
            if fqdn in skip_host:
                host_omitted = True
                continue
            if limit_hosts and (fqdn not in only_host):
                host_omitted = True
                continue
            dest_path = PurePath('/')
            
            sld('{}: {}'.format(cert.name, fqdn))
            
            for jail in ( dh['jails'].keys() or ('',) ):   # jail is empty if no jails
            
                jailroot = dh['jailroot'] if jail != '' else '' # may also be empty
                dest_path = PurePath('/', jailroot, jail)
                sld('{}: {}: {}'.format(cert.name, fqdn, dest_path))                
    
                if not dh['places']:
                    sle('{} subject has no place attribute.'.format(cert.name))
                    error_found = True
                    return False
                    
                for place in dh['places'].values():
                
                    sld('Handling jail "{}" and place {}'.format(jail, place.name))
                                   
                    fd_key = StringIO(key_text)
                    fd_cert = StringIO(cert_text)
                
                    key_file_name = key_name(cert.name, cert.subject_type)
                    cert_file_name = cert_name(cert.name, cert.subject_type)
                    
                    pcp = place.cert_path
                    if '{}' in pcp:     # we have a home directory named like the subject
                        pcp = pcp.format(cert.name)
                    dest_dir = PurePath(dest_path, pcp)
                
                    if place.key_path:
                        key_dest_dir = PurePath(dest_path, place.key_path)
                        distribute_cert(fd_key, fqdn, key_dest_dir, key_file_name, place, None)
                    
                    elif place.cert_file_type == 'separate':
                        distribute_cert(fd_key, fqdn, dest_dir, key_file_name, place, None)
                        if cert.cert_type == 'LE':
                            chain_file_name = cert_cacert_chain_name(cert.name, cert.subject_type)
                            fd_chain = StringIO(cert_text + cacert_text)
                            distribute_cert(fd_chain, fqdn, dest_dir, chain_file_name, place, jail)
                    
                    elif place.cert_file_type == 'combine key':
                        cert_file_name = key_cert_name(cert.name, cert.subject_type)
                        fd_cert = StringIO(key_text + cert_text)
                        if cert.cert_type == 'LE':
                            chain_file_name = cert_cacert_chain_name(cert.name, cert.subject_type)
                            fd_chain = StringIO(cert_text + cacert_text)
                            distribute_cert(fd_chain, fqdn, dest_dir, chain_file_name, place, jail)
                    
                    elif place.cert_file_type == 'combine both':
                        cert_file_name = key_cert_cacert_name(cert.name, cert.subject_type)
                        fd_cert = StringIO(key_text + cert_text + cacert_text)
                
                    elif place.cert_file_type == 'combine cacert':
                        cert_file_name = cert_cacert_name(cert.name, cert.subject_type)
                        fd_cert = StringIO(cert_text + cacert_text)
                        distribute_cert(fd_key, fqdn, dest_dir, key_file_name, place, None)
                    
                    distribute_cert(fd_cert, fqdn, dest_dir, cert_file_name, place, jail)
            
        sli('')
        if not opts.no_TLSA:
            distribute_tlsa_rrs(cert, TLSA_text, None)
        
        if not host_omitted:
            update_state_of_instance(cert.db, instance_id, 'deployed')
        else:
            sln('State of cert {} not promoted to DEPLOYED, '
                'because hosts where limized or skipped'.format(
                            cert.name))
        # clear mail-sent-time if local cert.
        if cert.cert_type == 'local': cert.update_authorized_until(None)
        
    updateSOAofUpdatedZones()
    reloadNameServer()
    return not error_found


def ssh_connection(dest_host):

    """
    Open a ssh connection.
    
    @param dest_host:   fqdn of target host
    @type dest_host:    string
    @rtype:             paramiko.SSHClient (connected transport)
    @exceptions:
    If unable to connect
    """

    client = SSHClient()
    client.load_host_keys(expanduser('~/.ssh/known_hosts'))
    sld('Connecting to {}'.format(dest_host))
    try:
        client.connect(dest_host, username=SSH_CLIENT_USER_NAME,
                            key_filename=expanduser('~/.ssh/id_rsa'))
    except Exception:
        sln('Failed to connect to host {}, because {} [{}]'.
            format(dest_host,
            sys.exc_info()[0].__name__,
            str(sys.exc_info()[1])))
        raise
    else:
        sld('Connected to host {}'.format(dest_host))
        return client

def distribute_cert(fd, dest_host, dest_dir, file_name, place, jail):

    """
    Distribute cert and key to a host, jail (if any) and place.
    Optional reload the service.
    
    @param fd:          file descriptor of memory stream
    @type fd:           io.StringIO
    @param dest_host:   fqdn of target host
    @type dest_host:    string
    @param dest_dir:    target directory
    @type dest_dir:     string
    @param file_name:   file name of key or cert file
    @type file_name:    string
    @param place:       place with details about setting mode and uid/gid of file
    @type place:        pki.cert.Place instance
    @param jail:        name of jail for service to reload
    @type jail:         string or None
    @rtype:             not yet any
    @exceptions:        IOError
    """

    with ssh_connection(dest_host) as client:
        
        with client.open_sftp() as sftp:
            try:
                sftp.chdir(str(dest_dir))
            except IOError:
                sln('{}:{} does not exist - creating\n\t{}'.format(
                            dest_host, dest_dir, sys.exc_info()[0].__name__))
                try:
                    sftp.mkdir(str(dest_dir))   
                except IOError:
                    sle('Cant create {}:{}: Missing parent?\n\t{}'.format(
                            dest_host,
                            dest_dir,
                            sys.exc_info()[0].__name__,
                            str(sys.exc_info()[1])))
                    raise
                sftp.chdir(str(dest_dir))
            
            sli('{} => {}:{}'.format(file_name, dest_host, dest_dir))
            fat = sftp.putfo(fd, file_name, confirm=True)
            sld('size={}, uid={}, gid={}, mtime={}'.format(
                        fat.st_size, fat.st_uid, fat.st_gid, fat.st_mtime))

            if 'key' in file_name:
                sld('Setting mode to 0o400 of {}:{}/{}'.format(
                                    dest_host, dest_dir, file_name))
                mode = 0o400
                # won't work 288(10) gives 400(8)
                if place.mode:
                    mode = place.mode
                    sln('Setting mode of key at target to 0o400 - should be {}'.format(oct(place.mode)))
                sftp.chmod(file_name, mode)
                if place.pgLink:
                    try:
                        sftp.unlink('postgresql.key')
                    except IOError:
                        pass            # none exists: ignore
                    sftp.symlink(file_name, 'postgresql.key')
                    sld('{} => postgresql.key'.format(file_name))
                 
            if 'key' in file_name or place.chownBoth:
                uid = gid = 0
                if place.uid: uid = place.uid
                if place.gid: gid = place.gid
                if uid != 0 or gid != 0:
                    sld('Setting uid/gid to {}:{} of {}:{}/{}'.format(
                                    uid, gid, dest_host, dest_dir, file_name))
                    sftp.chown(file_name, uid, gid)
            elif place.pgLink:
                try:
                    sftp.unlink('postgresql.crt')
                except IOError:
                    pass            # none exists: ignore
                sftp.symlink(file_name, 'postgresql.crt')
                sld('{} => postgresql.crt'.format(file_name))
        
        if jail and place.reload_command:
            try:
                cmd = str((place.reload_command).format(jail))
            except:             #No "{}" in reload command: means no jail
                cmd = place.reload_command
            sli('Executing "{}" on host {}'.format(cmd, dest_host))

            with client.get_transport().open_session() as chan:
                chan.settimeout(10.0)
                chan.set_combine_stderr(True)
                chan.exec_command(cmd)
                
                remote_result_msg = ''
                timed_out = False
                while not chan.exit_status_ready():
                     if timed_out: break
                     if chan.recv_ready():
                        try:
                            data = chan.recv(1024)
                        except (timeout):
                            sle('Timeout on remote execution of "{}" on host {}'.format(cmd, dest_host))
                            break
                        while data:
                            remote_result_msg += (data.decode('ascii'))
                            try:
                                data = chan.recv(1024)
                            except (timeout):
                                sle('Timeout on remote execution of "{}" on host {}'.format(cmd, dest_host))
                                tmp = timed_out
                                timed_out = True
                                break
                es = int(chan.recv_exit_status())
                if es != 0:
                    sln('Remote execution failure of "{}" on host {}\texit={}, because:\n\r{}'
                            .format(cmd, dest_host, es, remote_result_msg))
                else:
                    sli(remote_result_msg)


def key_name(subject, subject_type):
    return str('%s_%s_key.pem' % (subject, subject_type))

def cert_name(subject, subject_type):
    return str('%s_%s_cert.pem' % (subject, subject_type))

def cert_cacert_name(subject, subject_type):
    return str('%s_%s_cert_cacert.pem' % (subject, subject_type))

def cert_cacert_chain_name(subject, subject_type):
    return str('%s_%s_cert_cacert_chain.pem' % (subject, subject_type))

def key_cert_name(subject, subject_type):
    return str('%s_%s_key_cert.pem' % (subject, subject_type))

def key_cert_cacert_name(subject, subject_type):
    return str('%s_%s_key_cert_cacert.pem' % (subject, subject_type))




def distribute_tlsa_rrs(cert_meta, active_TLSA, prepublished_TLSA):
    
    """
    Distribute TLSA RR.
    Puts one (ore two) TLSA RR per fqdn in DNS zone directory and updates
    zone cache.
    @param cert_meta:   		Meta instance of certificates(s) being handled
    @type cert_meta:    		cert.Certificate instance
    @param active_TLSA:   		TLSA hash of active TLSA
    @type active_TLSA:    		string
    @param prepublished_TLSA:   TLSA hash of optional pre-published TLSA
    @type prepublished_TLSA:    string
    """

    if len(cert_meta.tlsaprefixes) == 0: return

    sli('Distributing TLSA RRs for DANE.')

    if Pathes.tlsa_dns_master == '':       # DNS master on local host
        for (zone, fqdn) in zone_and_FQDN_from_altnames(cert_meta): 
            filename = fqdn + '.tlsa'
            dest = str(Pathes.zone_file_root / zone / filename)
            sli('{} => {}'.format(filename, dest))
            tlsa_lines = []
            for prefix in cert_meta.tlsaprefixes:
                tlsa_lines.append(str(prefix.format(fqdn) +
                                         ' ' +active_TLSA + '\n'))
                if prepublished_TLSA:
                    tlsa_lines.append(str(prefix.format(fqdn) +
                                         ' ' +prepublished_TLSA + '\n'))
            with open(dest, 'w') as fd:
                fd.writelines(tlsa_lines)
            updateZoneCache(zone)

    else:                           # remote DNS master ( **INCOMPLETE**)
        sle('Remote DNS master server is currently not supported. Must be on same host as this script.')
        exit(1)
        with ssh_connection(Pathes.tlsa_dns_master) as client:
            with client.open_sftp() as sftp:
                chdir(str(Pathes.work_tlsa))
                p = Path('.')
                sftp.chdir(str(Pathes.zone_file_root))
                
                for child_dir in p.iterdir():
                    for child in child_dir.iterdir():
                        sli('{} => {}:{}'.format(
                                child, Pathes.tlsa_dns_master, child))
                        fat = sftp.put(str(child), str(child), confirm=True)
                        sld('size={}, uid={}, gid={}, mtime={}'.format(
                                        fat.st_size, fat.st_uid, fat.st_gid, fat.st_mtime))


