import sys
import signal
import time

from django.core.management.base import BaseCommand
from stellar_sdk.exceptions import BaseRequestError

from polaris import settings
from polaris.models import Transaction
from polaris.utils import getLogger
from polaris.integrations import registered_deposit_integration as rdi
from polaris.management.commands.poll_pending_deposits import (
    PendingDeposits,
    MultiSigTransactions,
)


logger = getLogger(__name__)
TERMINATE = False
DEFAULT_INTERVAL = 60


class Command(BaseCommand):
    """
    Create Stellar transaction for deposit transactions marked as pending trust, if a
    trustline has been created.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    @staticmethod
    def exit_gracefully(sig, frame):
        logger.info("Exiting check_trustlines...")
        module = sys.modules[__name__]
        module.TERMINATE = True

    @staticmethod
    def sleep(seconds):
        module = sys.modules[__name__]
        for _ in range(seconds):
            if module.TERMINATE:
                break
            time.sleep(1)

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Continually restart command after a specified number of seconds.",
        )
        parser.add_argument(
            "--interval",
            "-i",
            type=int,
            help="The number of seconds to wait before restarting command. "
            "Defaults to {}.".format(DEFAULT_INTERVAL),
        )

    def handle(self, *_args, **options):
        module = sys.modules[__name__]
        if options.get("loop"):
            while True:
                if module.TERMINATE:
                    break
                self.check_trustlines()
                self.sleep(options.get("interval") or DEFAULT_INTERVAL)
        else:
            self.check_trustlines()

    @staticmethod
    def check_trustlines():
        """
        Create Stellar transaction for deposit transactions marked as pending
        trust, if a trustline has been created.
        """
        module = sys.modules[__name__]
        transactions = Transaction.objects.filter(
            kind=Transaction.KIND.deposit, status=Transaction.STATUS.pending_trust
        )
        server = settings.HORIZON_SERVER
        accounts = {}
        for transaction in transactions:
            if module.TERMINATE:
                break
            if accounts.get(transaction.stellar_account):
                account = accounts[transaction.stellar_account]
            else:
                try:
                    account = (
                        server.accounts().account_id(transaction.stellar_account).call()
                    )
                    accounts[transaction.stellar_account] = account
                except BaseRequestError:
                    logger.exception(
                        f"Failed to load account {transaction.stellar_account}"
                    )
                    continue
            for balance in account["balances"]:
                if balance.get("asset_type") == "native":
                    continue
                if (
                    balance["asset_code"] == transaction.asset.code
                    and balance["asset_issuer"] == transaction.asset.issuer
                ):
                    logger.info(
                        f"Account {account['id']} has established a trustline for "
                        f"{balance['asset_code']}:{balance['asset_issuer']}"
                    )
                    if MultiSigTransactions.requires_multisig(transaction):
                        MultiSigTransactions.save_as_pending_signatures(transaction)
                        continue

                    if PendingDeposits.submit(transaction):
                        transaction.refresh_from_db()
                        try:
                            rdi.after_deposit(transaction)
                        except Exception:
                            logger.exception(
                                "An unexpected error was raised from "
                                "after_deposit() in check_trustlines"
                            )
