#!/usr/bin/python2

from collections import defaultdict
import email
import getpass
import logging

try:
    import argparse
except ImportError:
    import _argparse as argparse

from imaplib2 import IMAP4_SSL

GMAIL_HOST = 'imap.gmail.com'
IDLE_TIMEOUT = 3#00
LABELED_FLAG = 'Autolabeled'

class Autolabeler(object):
    def __init__(self, account, password, domain=None, mailbox=None, ignore=[],
                 label_prefix=None, label_case='lower', flag=LABELED_FLAG,
                 dry_run=False):
        
        self.account = account
        self.password = password
        self.domain = domain and domain.lower()
        self.mailbox = mailbox or 'INBOX'
        self.label_prefix = label_prefix or ''
        self.label_case = getattr(str, label_case, None)
        self.flag = '$%s' % flag
        self.dry_run = dry_run
        
        self.ignore = [self.account]
        self.ignore.extend(filter(None, ignore))
        
        self.criterion = ['NOT', 'KEYWORD', self.flag]
        if self.domain:
            self.criterion.extend(['TO', self.domain])

        self._resolve_cache = {}
            
    def run(self):
        try:
            self.connect()

            while 1:
                message_numbers = self.search(*self.criterion)
                if message_numbers:
                    messages = self.fetch(message_numbers)
                    self.handle_messages(messages)

                self.imap.idle(IDLE_TIMEOUT)
        finally:
            self.imap.logout()

    def connect(self):
        logging.debug('Connecting')
        
        self.imap = IMAP4_SSL(GMAIL_HOST)
        
        logging.debug('Logging in (%s)', self.account)
        
        self.imap.login(self.account, self.password)
        typ, data = self.imap.select(self.mailbox)
        if typ != 'OK':
            raise Exception(*data)

    def search(self, *criterion):
        logging.debug('Searching (%s)', ' '.join(criterion))

        typ, [data] = self.imap.search(None, *criterion)
        return data.split()
        
    def fetch(self, message_numbers):
        logging.debug('Fetching: %s', ','.join(message_numbers))
        
        typ, data = self.imap.fetch(
            ','.join(message_numbers), '(BODY[HEADER.FIELDS (TO CC BCC)])')

        for part, headers in data[::2]:
            yield part.split()[0], email.message_from_string(headers)
    
    def handle_messages(self, messages):
        to_flag = []
        labels = defaultdict(set)
        
        logging.debug('Handling messages:')
            
        for msg_num, msg in messages:
            addrs = [email.utils.parseaddr(addr)[1] for addr in msg.values()]
            
            logging.debug('Addresses [%s]: %s', msg_num, '; '.join(addrs))
        
            # TODO: replace with (configurable) regex?
            
            def ignore_filter(addr):
                logging.debug('Filtering: %s', addr)
                
                # Ensure this is a labelable address
                if self.domain:
                    if self.domain not in addr.lower():
                        return False
                elif '+' not in addr:
                    return False
                
                # Ensure this isn't an ignored address
                return not any(ign in addr for ign in self.ignore)
                
            # Find addresses to be labeled
            for addr in filter(ignore_filter, addrs):
                
                label = addr.split('@')[0]
                if not self.domain:
                    label = addr.split('+', 1)[1]
                
                # Transform label
                if self.label_case:
                    label = self.label_case(label)

                label = self.label_prefix + label
                    
                labels[label].add(msg_num)

        # Dry run - just display changes
        if self.dry_run:
            import pprint
            pprint.pprint(dict(labels))
            return
                
        # Label messages
        to_flag = []
        for label, msg_nums in labels.items():
            try:
                self.label_messages(label, msg_nums)
                to_flag.extend(msg_nums)
            except Exception, e:
                print 'Failed to label %s messages with label %s: %s' % (
                    len(msg_nums), label, e)

        # Flag messages as labeled
        if to_flag:
            self.imap.store(','.join(to_flag), '+FLAGS', self.flag)
                
    def label_messages(self, label, msg_nums, tried_create=False):
        typ, [reason] = self.imap.copy(','.join(msg_nums), label)
        if typ != 'OK':
            # Try to create label
            if 'TRYCREATE' in reason and not tried_create:
                typ, [reason] = self.imap.create(label)
                if typ != 'OK':
                    if 'conflict' in reason.lower():
                        label = self.resolve_conflict(label)
                    else:
                        raise Exception(reason)
                self.label_messages(label, msg_nums, tried_create=True)
            else:
                raise Exception(reason)
        
    def resolve_label(self, label):
        try:
            # Make sure cached labels still exist
            typ, _ = self.imap.status(self._resolve_cache[label], '(RECENT)')
            if typ != 'OK':
                # Cache is probably stale
                self._resolve_cache = {}
                raise KeyError
            
        except KeyError:
            # FIXME: just generate the whole (label.lower -> label) map at once
            # Cache miss
            typ, labels = self.imap.list()

            for test_label in labels:
                # Parse label entry
                if test_label[-1] == '"':
                    test_label = test_label.split(' "')[-1][:-1]
                else:
                    test_label = test_label.split()[-1]
                    
                    if test_label.lower() == label.lower():
                        self._resolve_cache[label] = test_label
                        break
                    else:
                        raise Exception('Failed to resolve label %s' % label)
                    
                    return self._resolve_cache[label]
    
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('account')
    parser.add_argument('-p', '--password')
    parser.add_argument('-d', '--domain')
    parser.add_argument('-m', '--mailbox')
    parser.add_argument('-i', '--ignore',
                        default='', type=lambda i: i.split(','))
    parser.add_argument('-e', '--label-prefix',
                        default='Autolabeler/')
    parser.add_argument('-c', '--label-case',
                        choices='lower upper capitalize none'.split())
    parser.add_argument('-y', '--dry-run',
                        action='store_true')

    parser.add_argument('-v', '--verbose', dest='_verbose',
                        action='store_true')
    
    args = parser.parse_args()

    if args._verbose:
        import sys
        logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
    
    if '@' not in args.account:
        args.account = '%s@gmail.com' % args.account
        
    args.password = args.password or getpass.getpass()
    
    # Get all non-empty args that don't start with _
    kwargs = dict(
        (k, v) for k, v in args.__dict__.items() if k[0] != '_' and v)
    
    Autolabeler(**kwargs).run()
