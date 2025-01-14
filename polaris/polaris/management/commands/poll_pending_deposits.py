import sys
import signal
import time
import datetime
import base64
from decimal import Decimal
from typing import Tuple, List, Optional

from django.core.management import BaseCommand, CommandError
from stellar_sdk import Keypair, TransactionEnvelope, Asset, Claimant
from stellar_sdk.account import Account
from stellar_sdk.exceptions import BaseHorizonError, ConnectionError
from stellar_sdk.transaction_builder import TransactionBuilder
from stellar_sdk.xdr.StellarXDR_type import TransactionResult

from polaris import settings
from polaris.utils import get_account_obj, is_pending_trust, maybe_make_callback
from polaris.integrations import (
    registered_deposit_integration as rdi,
    registered_rails_integration as rri,
    registered_fee_func,
    calculate_fee,
)
from polaris.models import Transaction
from polaris.utils import getLogger, make_memo

logger = getLogger(__name__)
TERMINATE = False
DEFAULT_INTERVAL = 10


class PendingDeposits:
    @staticmethod
    def get_ready_deposits() -> List[Transaction]:
        pending_deposits = Transaction.objects.filter(
            status__in=[
                Transaction.STATUS.pending_user_transfer_start,
                Transaction.STATUS.pending_external,
            ],
            kind=Transaction.KIND.deposit,
        )
        ready_transactions = rri.poll_pending_deposits(pending_deposits)
        for transaction in ready_transactions:
            if transaction.kind != Transaction.KIND.deposit:
                raise ValueError(
                    "A non-deposit Transaction was returned from poll_pending_deposits()"
                )
            elif transaction.amount_in is None:
                raise ValueError(
                    "poll_pending_deposits() did not assign a value to the "
                    "amount_in field of a Transaction object returned"
                )
            elif transaction.amount_fee is None:
                if registered_fee_func is calculate_fee:
                    transaction.amount_fee = calculate_fee(
                        {
                            "amount": transaction.amount_in,
                            "operation": settings.OPERATION_DEPOSIT,
                            "asset_code": transaction.asset.code,
                        }
                    )
                else:
                    transaction.amount_fee = Decimal(0)
                transaction.save()
        return ready_transactions

    @staticmethod
    def get_or_create_destination_account(
        transaction: Transaction,
    ) -> Tuple[Account, bool]:
        """
        Returns:
            Account: The account found or created for the Transaction
            bool: True if trustline doesn't exist, False otherwise.

        If the account doesn't exist, Polaris must create the account using an account provided by the
        anchor. Polaris can use the distribution account of the anchored asset or a channel account if
        the asset's distribution account requires non-master signatures.

        If the transacted asset's distribution account does require non-master signatures,
        DepositIntegration.create_channel_account() will be called. See the function docstring for more
        info.

        On failure to create the destination account, a RuntimeError exception is raised.
        """
        try:
            account, json_resp = get_account_obj(
                Keypair.from_public_key(transaction.stellar_account)
            )
            return account, is_pending_trust(transaction, json_resp)
        except RuntimeError:
            if MultiSigTransactions().requires_multisig(transaction):
                source_account_kp = MultiSigTransactions.get_channel_keypair(
                    transaction
                )
                source_account, _ = get_account_obj(source_account_kp)
            else:
                source_account_kp = Keypair.from_secret(
                    transaction.asset.distribution_seed
                )
                source_account, _ = get_account_obj(source_account_kp)

            builder = TransactionBuilder(
                source_account=source_account,
                network_passphrase=settings.STELLAR_NETWORK_PASSPHRASE,
                # this transaction contains one operation so base_fee will be multiplied by 1
                base_fee=settings.MAX_TRANSACTION_FEE_STROOPS
                or settings.HORIZON_SERVER.fetch_base_fee(),
            )
            transaction_envelope = builder.append_create_account_op(
                destination=transaction.stellar_account,
                starting_balance=settings.ACCOUNT_STARTING_BALANCE,
            ).build()
            transaction_envelope.sign(source_account_kp)

            try:
                settings.HORIZON_SERVER.submit_transaction(transaction_envelope)
            except BaseHorizonError as submit_exc:  # pragma: no cover
                raise RuntimeError(
                    "Horizon error when submitting create account to horizon: "
                    f"{submit_exc.message}"
                )

            account, _ = get_account_obj(
                Keypair.from_public_key(transaction.stellar_account)
            )
            return account, True
        except BaseHorizonError as e:
            raise RuntimeError(
                f"Horizon error when loading stellar account: {e.message}"
            )

    @classmethod
    def submit(cls, transaction: Transaction) -> bool:
        valid_statuses = [
            Transaction.STATUS.pending_user_transfer_start,
            Transaction.STATUS.pending_external,
            Transaction.STATUS.pending_anchor,
            Transaction.STATUS.pending_trust,
        ]
        if transaction.status not in valid_statuses:
            raise ValueError(
                f"Unexpected transaction status: {transaction.status}, expecting "
                f"{' or '.join(valid_statuses)}."
            )

        transaction.status = Transaction.STATUS.pending_anchor
        transaction.status_eta = 5  # Ledger close time.
        transaction.save()
        logger.info(f"Initiating Stellar deposit for {transaction.id}")
        maybe_make_callback(transaction)

        if transaction.envelope_xdr:
            envelope = TransactionEnvelope.from_xdr(
                transaction.envelope_xdr, settings.STELLAR_NETWORK_PASSPHRASE
            )
        else:
            distribution_acc, _ = get_account_obj(
                Keypair.from_public_key(transaction.asset.distribution_account)
            )
            envelope = cls.create_deposit_envelope(transaction, distribution_acc)
            envelope.sign(transaction.asset.distribution_seed)

        transaction.status = Transaction.STATUS.pending_stellar
        transaction.save()
        logger.info(f"Transaction {transaction.id} now pending_stellar")
        maybe_make_callback(transaction)

        try:
            response = settings.HORIZON_SERVER.submit_transaction(envelope)
        except BaseHorizonError as e:
            cls._handle_error(transaction, f"{e.__class__.__name__}: {e.message}")
            return False

        if not response.get("successful"):
            cls._handle_error(
                transaction,
                f"Stellar transaction failed when submitted to horizon: {response['result_xdr']}",
            )
            return False
        elif transaction.claimable_balance_supported:
            transaction.claimable_balance_id = cls.get_balance_id(response)

        transaction.envelope_xdr = response["envelope_xdr"]
        transaction.paging_token = response["paging_token"]
        transaction.stellar_transaction_id = response["id"]
        transaction.status = Transaction.STATUS.completed
        transaction.completed_at = datetime.datetime.now(datetime.timezone.utc)
        transaction.status_eta = 0
        transaction.amount_out = round(
            Decimal(transaction.amount_in) - Decimal(transaction.amount_fee),
            transaction.asset.significant_decimals,
        )
        transaction.save()
        logger.info(f"Transaction {transaction.id} completed.")
        maybe_make_callback(transaction)
        return True

    @staticmethod
    def create_deposit_envelope(transaction, source_account) -> TransactionEnvelope:
        payment_amount = round(
            Decimal(transaction.amount_in) - Decimal(transaction.amount_fee),
            transaction.asset.significant_decimals,
        )
        builder = TransactionBuilder(
            source_account=source_account,
            network_passphrase=settings.STELLAR_NETWORK_PASSPHRASE,
            # only one operation, so base_fee will be multipled by 1
            base_fee=settings.MAX_TRANSACTION_FEE_STROOPS
            or settings.HORIZON_SERVER.fetch_base_fee(),
        )
        _, json_resp = get_account_obj(
            Keypair.from_public_key(transaction.stellar_account)
        )
        if transaction.claimable_balance_supported and is_pending_trust(
            transaction, json_resp
        ):
            logger.debug(
                f"Crafting claimable balance operation for Transaction {transaction.id}"
            )
            claimant = Claimant(destination=transaction.stellar_account)
            asset = Asset(code=transaction.asset.code, issuer=transaction.asset.issuer)
            builder.append_create_claimable_balance_op(
                claimants=[claimant],
                asset=asset,
                amount=str(payment_amount),
                source=transaction.asset.distribution_account,
            )
        else:
            builder.append_payment_op(
                destination=transaction.stellar_account,
                asset_code=transaction.asset.code,
                asset_issuer=transaction.asset.issuer,
                amount=str(payment_amount),
                source=transaction.asset.distribution_account,
            )
        if transaction.memo:
            builder.add_memo(make_memo(transaction.memo, transaction.memo_type))
        return builder.build()

    @staticmethod
    def get_balance_id(response: dict) -> Optional[str]:
        """
        Pulls claimable balance ID from horizon responses if present

        When called we decode and read the result_xdr from the horizon response.
        If any of the operations is a createClaimableBalanceResult we
        decode the Base64 representation of the balanceID xdr.
        After the fact we encode the result to hex.

        The hex representation of the balanceID is important because its the
        representation required to query and claim claimableBalances.

        :param
            response: the response from horizon

        :return
            hex representation of the balanceID
            or
            None (if no createClaimableBalanceResult operation is found)
        """
        result_xdr = response["result_xdr"]
        balance_id_hex = None
        for op_result in TransactionResult.from_xdr(result_xdr).result.results:
            if hasattr(op_result.tr, "createClaimableBalanceResult"):
                balance_id_hex = base64.b64decode(
                    op_result.tr.createClaimableBalanceResult.balanceID.to_xdr()
                ).hex()
        return balance_id_hex

    @classmethod
    def _handle_error(cls, transaction, message):
        transaction.status_message = message
        transaction.status = Transaction.STATUS.error
        transaction.save()
        logger.error(transaction.status_message)
        maybe_make_callback(transaction)


class MultiSigTransactions:
    @staticmethod
    def requires_multisig(transaction: Transaction) -> bool:
        master_signer = None
        if transaction.asset.distribution_account_master_signer:
            master_signer = transaction.asset.distribution_account_master_signer
        thresholds = transaction.asset.distribution_account_thresholds
        return (
            not master_signer or master_signer["weight"] < thresholds["med_threshold"]
        )

    @staticmethod
    def get_channel_keypair(transaction) -> Keypair:
        if not transaction.channel_account:
            rdi.create_channel_account(transaction)
        return Keypair.from_secret(transaction.channel_seed)

    @classmethod
    def save_as_pending_signatures(cls, transaction):
        channel_kp = cls.get_channel_keypair(transaction)
        try:
            channel_account, _ = get_account_obj(channel_kp)
        except RuntimeError as e:
            transaction.status = Transaction.STATUS.error
            transaction.status_message = str(e)
            transaction.save()
            logger.error(transaction.status_message)
        else:
            # Create the initial envelope XDR with the channel signature
            envelope = PendingDeposits.create_deposit_envelope(
                transaction, channel_account
            )
            envelope.sign(channel_kp)
            transaction.envelope_xdr = envelope.to_xdr()
            transaction.pending_signatures = True
            transaction.status = Transaction.STATUS.pending_anchor
            transaction.save()
        maybe_make_callback(transaction)


class Command(BaseCommand):
    """
    Polls the anchor's financial entity, gathers ready deposit transactions
    for execution, and executes them. This process can be run in a loop,
    restarting every 10 seconds (or a user-defined time period)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    @staticmethod
    def exit_gracefully(sig, frame):
        logger.info("Exiting poll_pending_deposits...")
        module = sys.modules[__name__]
        module.TERMINATE = True

    @staticmethod
    def sleep(seconds):
        module = sys.modules[__name__]
        for _ in range(seconds):
            if module.TERMINATE:
                break
            time.sleep(1)

    def add_arguments(self, parser):  # pragma: no cover
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

    def handle(self, *_args, **options):  # pragma: no cover
        module = sys.modules[__name__]
        if options.get("loop"):
            while True:
                if module.TERMINATE:
                    break
                self.execute_deposits()
                self.sleep(options.get("interval") or DEFAULT_INTERVAL)
        else:
            self.execute_deposits()

    @classmethod
    def execute_deposits(cls):
        module = sys.modules[__name__]
        try:
            ready_transactions = PendingDeposits.get_ready_deposits()
        except Exception:
            logger.exception("poll_pending_deposits() threw an unexpected exception")
            return
        if not isinstance(ready_transactions, list):
            raise CommandError(
                "poll_pending_deposits() did not return a list of transaction objects."
            )
        for transaction in ready_transactions:
            if module.TERMINATE:
                break
            cls.execute_deposit(transaction)

        multisig_transactions = Transaction.objects.filter(
            kind=Transaction.KIND.deposit,
            status=Transaction.STATUS.pending_anchor,
            pending_signatures=False,
            envelope_xdr__isnull=False,
        )
        for transaction in multisig_transactions:
            if PendingDeposits.submit(transaction):
                transaction.refresh_from_db()
                try:
                    rdi.after_deposit(transaction)
                except Exception:
                    logger.exception("after_deposit() threw an unexpected exception")

    @classmethod
    def execute_deposit(cls, transaction):
        try:
            _, pending_trust = PendingDeposits.get_or_create_destination_account(
                transaction
            )
        except RuntimeError as e:
            transaction.status = Transaction.STATUS.error
            transaction.status_message = str(e)
            transaction.save()
            logger.error(transaction.status_message)
            maybe_make_callback(transaction)
            return

        if pending_trust and not transaction.claimable_balance_supported:
            logger.info(
                f"destination account is pending_trust for transaction {transaction.id}"
            )
            transaction.status = Transaction.STATUS.pending_trust
            transaction.save()
            maybe_make_callback(transaction)
            return
        if MultiSigTransactions.requires_multisig(transaction):
            MultiSigTransactions.save_as_pending_signatures(transaction)
            return
        if PendingDeposits.submit(transaction):
            transaction.refresh_from_db()
            try:
                rdi.after_deposit(transaction)
            except Exception:
                logger.exception("after_deposit() threw an unexpected exception")
