from __future__ import print_function
import dateutil.tz
import dateutil.parser
from pycoin.tx import Tx
from pycoin.ecdsa import generator_secp256k1
from pycoin.serialize import b2h, stream_to_bytes
from hierarchy import *
from pycoin.tx.script.tools import *
from pycoin.tx.script import der
import json
import requests
import uuid

__author__ = 'sserrano, devrandom'


class Error(Exception):
    pass


class OracleError(Error):
    pass

class OracleInternalError(Error):
    pass

class OracleException(Exception):
    pass

class OracleRejectionException(OracleError):
    """Rejected transaction due to user cancel or business rule violation"""
    pass

class OracleLockoutException(OracleError):
    """Rejected transaction due to account or keychain being locked out due to user request or other reason"""
    pass

class OracleUnknownKeychainException(OracleException):
    """Got error 404 on oracle /keychains/<uuid>"""
    pass

class OracleDeferralException(OracleException):
    """Deferred transaction due to required verifications and/or delay"""

    def __init__(self, verifications, until, spend_id):
        self._verifications = verifications
        self._until = until
        self._spend_id = spend_id

    @property
    def verifications(self):
        """a list of required verifications, such as 'code' (SMS code) and 'otp'
        :returns list of str"""
        return self._verifications

    @property
    def until(self):
        """if there a delay is required, this will contain the time until the delay expires
        :returns datetime.datetime"""
        return self._until

    @property
    def spend_id(self):
        """the Oracle id for this spend
        :returns str"""
        return self._spend_id


class SignatureResult(dict):
    def __init__(self, value):
        super(SignatureResult, self).__init__(**value)
        self.__dict__ = self


class Oracle(object):
    """Keep track of a single Oracle account, including user keys and oracle master public key"""

    def __init__(self, account, tx_db=None, manager=None, base_url=None, num_oracle_keys=1):
        """
        Create an Oracle object

        :param account: multisig account, may be incomplete if the oracle key is not known yet (pending create/get)
        :type account: MultisigAccount
        :param tx_db: lookup database for transactions - see pycoin.services.get_tx_db()
        :param manager: the manager identifier for this wallet (only used on creation for now)
        """
        self._account = account
        self.manager = manager
        self._wallet_agent = 'multisig-core-0.01'
        self.tx_db = tx_db
        self.base_url = base_url or 'https://s.digitaloracle.co/'
        self.num_oracle_keys = num_oracle_keys
        self.verbose = 0

    @property
    def account(self):
        """The multisig account.  May be incomplete if we did not yet create or get the oracle key.
        :returns hierarchy.MultisigAccount"""
        return self._account

    @property
    def wallet_agent(self):
        return self._wallet_agent

    @wallet_agent.getter
    def set_wallet_agent(self, agent):
        self._wallet_agent = agent

    def _create_oracle_request(self, input_chain_paths, output_chain_paths, spend_id, tx, verifications=None):
        """:nodoc:"""
        # Have the Oracle sign the tx
        chain_paths = []
        input_scripts = []
        input_txs = []
        for i, inp in enumerate(tx.txs_in):
            input_tx = self.tx_db.get(inp.previous_hash)
            if input_tx is None:
                raise Error("could not look up tx for %s" % (b2h(inp.previous_hash)))
            input_txs.append(input_tx)
            if input_chain_paths[i] is not None:
                redeem_script = self._account.script_for_path(input_chain_paths[i]).script()
                input_scripts.append(redeem_script)
                chain_paths.append(input_chain_paths[i])
                fix_input_script(inp, redeem_script)
            else:
                input_scripts.append(None)
                chain_paths.append(None)
        req = {
            "walletAgent": self._wallet_agent,
            "transaction": {
                "bytes": b2h(stream_to_bytes(tx.stream)),
                "inputScripts": [(b2h(script) if script else None) for script in input_scripts],
                "inputTransactions": [b2h(stream_to_bytes(tx.stream)) for tx in input_txs],
                "chainPaths": chain_paths,
                "outputChainPaths": output_chain_paths,
                "masterKeys": self._account.public_keys[0:-self.num_oracle_keys],
            }
        }
        if spend_id:
            req['spendId'] = spend_id
        if verifications:
            req['verifications'] = verifications
        return req

    def sign(self, tx, input_leafs, output_leafs, spend_id=None, verifications=None):
        """
        Have the Oracle sign the transaction

        :param tx: the transaction to be signed
        :type tx: Tx
        :param input_leafs: the input leaf payto for each input, or None if the input does not need to be signed
        :type input_leafs: list[LeafPayTo or None]
        :param output_leafs: the output leaf payto, or None if the output is not change
        :type input_leafs: list[LeafPayTo or None]
        :param spend_id: an additional hex ID to disambiguate sends to the same outputs
        :type spend_id: str
        :param verifications: an optional dictionary with authorization code for each verification type.  Keys include "otp" and "code" (for SMS).
        :type dict of [str, str]
        :return: a dictionary with the transaction in 'transaction' if successful
        :rtype: dict
        """
        input_chain_paths = [x.path if x else None for x in input_leafs]
        output_chain_paths = [x.path if x else None for x in output_leafs]
        return self.sign_with_paths(tx, input_chain_paths, output_chain_paths, spend_id, verifications)

    def sign_with_paths(self, tx, input_chain_paths, output_chain_paths, spend_id=None, verifications=None):
        """
        Have the Oracle sign the transaction

        :param tx: the transaction to be signed
        :type tx: Tx
        :param input_chain_paths: the derivation path for each input, or None if the input does not need to be signed
        :type input_chain_paths: list[str or None]
        :param output_chain_paths: the derivation path for each change output, or None if the output is not change
        :type output_chain_paths: list[str or None]
        :param spend_id: an additional hex ID to disambiguate sends to the same outputs
        :type spend_id: str
        :param verifications: an optional dictionary with authorization code for each verification type.  Keys include "otp" and "code" (for SMS).
        :type dict of [str, str]
        :return: a dictionary with the transaction in 'transaction' if successful
        :rtype: dict
        """
        req = self._create_oracle_request(input_chain_paths, output_chain_paths, spend_id, tx, verifications)
        body = json.dumps(req)
        url = self._url() + "/transactions"
        if self.verbose > 0:
            print(body)
        response = requests.post(url, body, headers={'content-type': 'application/json'})
        if response.status_code >= 500:
            raise OracleInternalError(response.content)
        result = response.json()
        if response.status_code == 200 and result.get('result', None) == 'success':
            tx = None
            if 'transaction' in result:
                tx = Tx.tx_from_hex(result['transaction']['bytes'])
            return SignatureResult({
                'transaction': tx,
                'now': result['now'],
                'spend_id': result['spendId'],
                'deferral': result.get('deferral')
            })
        if result.get('result') == 'deferred':
            deferral = result['deferral']
            until = None
            if deferral and deferral['reason'] == 'delay':
                tzlocal = dateutil.tz.tzlocal()
                until = dateutil.parser.parse(deferral['until']).astimezone(tzlocal)
                #remain = int((until - datetime.datetime.now(tzlocal)).total_seconds())
            raise OracleDeferralException(deferral.get('verifications'), until, result['spendId'])
        elif result.get('result') == 'rejected':
            raise OracleRejectionException()
        elif result.get('result') == 'locked':
            raise OracleLockoutException()
        elif response.status_code == 200 or response.status_code == 400:
            raise OracleError(response.content)
        else:
            raise IOError("Unknown response %d" % (response.status_code,))

    def _url(self):
        account_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:digitaloracle.co:%s" % (self._account.public_keys[0])))
        url = self.base_url + "keychains/" + account_id
        return url

    def get(self):
        """Retrieve the oracle public key from the Oracle"""
        if self._account.complete:
            raise Exception("the account for this Oracle is already complete")
        url = self._url()
        response = requests.get(url)
        result = response.json()
        if response.status_code == 200 and result.get('result', None) == 'success':
            self._account.add_keys([AccountKey.from_key(s) for s in result['keys']['default']])
            self.num_oracle_keys = len(result['keys']['default'])
            self._account.set_complete()
        elif response.status_code == 200 or response.status_code == 400:
            raise OracleError(response.content)
        elif response.status_code == 404:
            raise OracleUnknownKeychainException("No keychain found on %s" % (url,))
        else:
            raise Error("Unknown response %d" % (response.status_code,))

    def create(self, parameters, email=None, phone=None):
        """
        Create an Oracle keychain on server and retrieve the oracle public key.

        :param email: the email contact
        :type email: str or unicode
        :param phone: the phone contact
        :type phone: str or unicode

        Example security parameters::
            "parameters": {
                "levels": [ {
                    "asset": "BTC",
                    "period": 3600,
                    "value": 1.0
                }, {
                    "delay": 0,
                    "calls": ['phone', 'email']
                }, ],
                "authenticator": {
                    "firstValue": "123456",
                    "secret": "aaaaaaaaaaaaaaaaaaaaaaaa",
                    "type": "totp"
                }
           }
        """
        if self._account.complete:
            raise Exception("account already complete")
        r = {'walletAgent': self._wallet_agent, 'rulesetId': 'default'}
        if self.manager:
            r['managerUsername'] = self.manager
        r['pii'] = {}
        if email:
            r['pii']['email'] = email
        if email:
            r['pii']['phone'] = phone
        r['parameters'] = parameters
        r['keys'] = [k.hwif() for k in self._account.keys]
        body = json.dumps(r)
        url = self._url()
        response = requests.post(url, body, headers={'content-type': 'application/json'})

        result = response.json()
        if response.status_code == 200 and result.get('result', None) == 'success':
            self._account.add_keys([AccountKey.from_key(s) for s in result['keys']['default']])
            self.num_oracle_keys = len(result['keys']['default'])
            self._account.set_complete()
        elif response.status_code == 400 and result.get('error', None) == 'already exists':
            raise OracleError("already exists")
        elif response.status_code == 200 or response.status_code == 400:
            raise OracleError(response.content)
        else:
            print(body)
            print(response.content)
            raise Error("Unknown response %d" % (response.status_code,))


def dummy_signature(sig_type):
    order = generator_secp256k1.order()
    r, s = order - 1, order // 2
    return der.sigencode_der(r, s) + bytes_from_int(sig_type)


def fix_input_script(inp, redeem_script):
    """replace dummy signatures with OP_0 and add redeem script for digitaloracle compatibility"""
    dummy = b2h(dummy_signature(1))
    ops1 = []
    for op in opcode_list(inp.script):
        if op == dummy:
            op = 'OP_0'
        ops1.append(op)
    # FIXME hack to add redeem script omitted by pycoin
    ops1.append(b2h(redeem_script))
    inp.script = compile(' '.join(ops1))