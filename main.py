#!/usr/bin/python2

from collections import defaultdict
import email
import getpass
import logging
import itertools
import sys
import types

try:
    import argparse
except ImportError:
    import _argparse as argparse

from imaplib2 import IMAP4_SSL

GMAIL_HOST = 'imap.gmail.com'
IDLE_TIMEOUT = 300
LABELED_FLAG = 'Autolabeled'
CHUNK_SIZE = 100

def iter_chunks(iterable, size):
    i = iter(iterable)
    while 1:
        chunk = list(itertools.islice(i, size))
        if chunk:
            yield chunk
        else:
            break

class Autolabeler(object):
    def __init__(self, account, password, domain=[], mailbox=None, ignore=[],
                 label_prefix=None, label_case='lower', flag=LABELED_FLAG,
                 idle_timeout=IDLE_TIMEOUT, dry_run=False, move=False):
        
        self.account = account
        self.password = password
        self.mailbox = mailbox or 'INBOX'
        self.label_case = getattr(str, label_case, None)
        self.flag = '$%s' % flag
        self.dry_run = dry_run
        self.move = move
        self.label_prefix = label_prefix
        
        if isinstance(domain, types.StringTypes):
            domain = [domain]
        self.domain = domain
        
        self.ignore = []
        if ignore:
            self.ignore.extend(i.lower() for i in ignore if i)
        elif self.domain:
            self.ignore.append(account.split('@')[0].lower())
        
        self.criterion = ['NOT', 'KEYWORD', self.flag]
        if self.domain:
            for domain in self.domain[1:]:
                self.criterion.extend(['OR', 'TO', domain])
            self.criterion.extend(['TO', self.domain[0]])

    def run(self):
        try:
            self.connect()

            while 1:
                message_numbers = self.search(*self.criterion)
                for number_chunk in iter_chunks(message_numbers, CHUNK_SIZE):
                    messages = self.fetch(number_chunk)
                    self.handle_messages(messages)

                if self.dry_run:
                    break
                        
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
        typ, [data] = self.imap.search(None, *criterion)
        return data.split()
        
    def fetch(self, message_numbers):
        typ, data = self.imap.fetch(
            ','.join(message_numbers), '(BODY[HEADER.FIELDS (TO CC BCC)])')

        for part, headers in data[::2]:
            yield part.split()[0], email.message_from_string(headers)
    
    def handle_messages(self, messages):
        to_flag = []
        labels = defaultdict(set)
        
        for msg_num, msg in messages:
            addrs = [email.utils.parseaddr(addr)[1] for addr in msg.values()]
            
            logging.debug('Addresses [%s]: %s', msg_num, '; '.join(addrs))
        
            # TODO: replace with (configurable) regex?
            
            def label_from_addr(addr):
                local, domain = addr.split('@')
                if self.domain:
                    domain = domain.lower()
                    for test_domain in self.domain:
                        if domain == test_domain.lower():
                            return local, test_domain
                elif '+' in local:
                    return local.split('+', 1)[1], None
                return None, None
                
            for label, domain in map(label_from_addr, addrs):
                # Check ignore list
                if not label or label.lower() in self.ignore:
                    continue
            
                # Transform label
                if self.label_case:
                    label = self.label_case(label)
                    
                # Apply label prefix
                if self.label_prefix:
                    if domain and '%' in self.label_prefix:
                        label = self.label_prefix.replace('%', domain, 1) + label
                    else:
                        label = self.label_prefix + label

                labels[label].add(msg_num)

        # Dry run - just display labels
        if self.dry_run:
            if labels:
                print 'Dry run:'
                for label, msgs in sorted(labels.items()):
                    print '%s: %s' % (label, ', '.join(msgs))
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
        # Add label by copying message into label
        msg_num_str = ','.join(msg_nums)
        typ, [reason] = self.imap.copy(msg_num_str, label)
        if typ != 'OK':
            # Try to create label
            if 'TRYCREATE' in reason and not tried_create:
                typ, [reason] = self.imap.create(label)
                if typ != 'OK':
                    if 'conflict' in reason.lower():
                        # Probably different string case
                        label = self.resolve_conflict(label)
                    else:
                        raise Exception(reason)
                self.label_messages(label, msg_nums, tried_create=True)
            else:
                raise Exception(reason)
        elif self.move:
            # Remove messages from current label
            self.imap.store(msg_num_str, '+FLAGS', r'\Deleted')
            self.imap.expunge()
            
    def resolve_label(self, label):
        label = label.lower()
        try:
            # Check and validate cache
            typ, _ = self.imap.status(self._resolve_cache[label], '(RECENT)')
            if typ != 'OK':
                # Invalidate cache
                raise KeyError
            
        except (AttributeError, KeyError):
            # (Re)generate label case cache
            typ, labels = self.imap.list()

            def parse_label(lab):
                if lab[-1] == '"':
                    # Quoted
                    return lab.split(' "')[-1][:-1]
                else:
                    return lab.split()[-1]
            
            self._resolve_cache = dict(
                (l.lower(), l) for l in map(parse_label, labels))
                
        return self._resolve_cache[label]
    
if __name__ == '__main__':
    parser = argparse.ArgumentParser(argument_default=argparse.SUPPRESS)
    parser.add_argument('-v', '--verbose', 
                        dest='_verbose', action='store_true', default=False)
    parser.add_argument('-y', '--dry-run', action='store_true',
                        help='Just print labels that would be applied')
    
    parser.add_argument('account',
                        help='Google mail account')
    parser.add_argument('-p', '--password', default=None,
                        help='Account password (will prompt otherwise)')
    parser.add_argument('-m', '--mailbox',
                        help='Mailbox to label (defaults to Inbox)')
    parser.add_argument('-M', '--move', action='store_true',
                        help='Move labeled messages out of mailbox')
    
    labeling_type = parser.add_mutually_exclusive_group()
    labeling_type.add_argument('-t', '--tags', action='store_const', const=None,
                        help='Label for address tags (plus-style) [default]')
    labeling_type.add_argument('-d', '--domain', type=lambda i: i.split(','),
                        metavar='DOMAIN[,DOMAIN...]',
                        help='Label for "catch-all" domain addresses')
    parser.add_argument('-i', '--ignore', type=lambda i: i.split(','),
                        metavar='IGNORE[,IGNORE...]',
                        help='Comma-separated list of labels to ignore')
    parser.add_argument('-f', '--label-prefix',
                        help='Prefix to add to all labels (use "%" for domain)')
    parser.add_argument('-c', '--label-case', default='lower',
                        choices='lower upper capitalize none'.split(),
                        help='Transform label case (Default: %(default)s)')
    
    args = parser.parse_args()
    
    if args._verbose:
        import sys
        logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
    
    if '@' not in args.account:
        args.account = '%s@gmail.com' % args.account
        
    args.password = args.password or getpass.getpass()
    
    # Get all non-empty args that don't start with _
    kwargs = dict(
        (k, v) for k, v in args.__dict__.items() if k[0] != '_')

    Autolabeler(**kwargs).run()
