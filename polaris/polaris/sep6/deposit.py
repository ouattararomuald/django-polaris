from typing import Dict, Tuple, Optional

from django.utils.translation import gettext as _
from rest_framework.decorators import api_view, renderer_classes, parser_classes
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.renderers import JSONRenderer, BrowsableAPIRenderer
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.exceptions import APIException
from stellar_sdk.exceptions import MemoInvalidException

from polaris.models import Asset, Transaction
from polaris.locale.utils import validate_language, activate_lang_for_request
from polaris.utils import (
    getLogger,
    render_error_response,
    create_transaction_id,
    extract_sep9_fields,
    make_memo,
)
from polaris.sep6.utils import validate_403_response
from polaris.sep10.utils import validate_sep10_token
from polaris.integrations import (
    registered_deposit_integration as rdi,
    registered_fee_func,
    calculate_fee,
)


logger = getLogger(__name__)


@api_view(["GET"])
@renderer_classes([JSONRenderer, BrowsableAPIRenderer])
@parser_classes([MultiPartParser, FormParser, JSONParser])
@validate_sep10_token()
def deposit(account: str, client_domain: Optional[str], request: Request,) -> Response:
    args = parse_request_args(request)
    if "error" in args:
        return args["error"]
    args["account"] = account

    transaction = Transaction(
        id=create_transaction_id(),
        stellar_account=account,
        asset=args["asset"],
        kind=Transaction.KIND.deposit,
        status=Transaction.STATUS.pending_user_transfer_start,
        memo=args["memo"],
        memo_type=args["memo_type"] or Transaction.MEMO_TYPES.text,
        to_address=account,
        protocol=Transaction.PROTOCOL.sep6,
        client_domain=client_domain,
    )

    try:
        integration_response = rdi.process_sep6_request(args, transaction)
    except ValueError as e:
        return render_error_response(str(e))
    except APIException as e:
        return render_error_response(str(e), status_code=e.status_code)

    try:
        response, status_code = validate_response(
            args, integration_response, transaction
        )
    except (ValueError, KeyError):
        return render_error_response(
            _("unable to process the request"), status_code=500
        )

    if status_code == 200:
        logger.info(f"Created deposit transaction {transaction.id}")
        transaction.save()
    elif Transaction.objects.filter(id=transaction.id).exists():
        logger.error("Do not save transaction objects for invalid SEP-6 requests")
        return render_error_response(
            _("unable to process the request"), status_code=500
        )

    return Response(response, status=status_code)


def validate_response(
    args: Dict, integration_response: Dict, transaction: Transaction
) -> Tuple[Dict, int]:
    """
    Validate /deposit response returned from integration function
    """
    if "type" in integration_response:
        return validate_403_response(integration_response, transaction), 403

    asset = args["asset"]
    response = {
        "min_amount": round(asset.deposit_min_amount, asset.significant_decimals),
        "max_amount": round(asset.deposit_max_amount, asset.significant_decimals),
        "how": integration_response["how"],
    }
    if not isinstance(response["how"], str):
        logger.error("Invalid 'how' returned from process_sep6_request()")
        raise ValueError()

    if calculate_fee == registered_fee_func:
        # Polaris user has not replaced default fee function, so fee_fixed
        # and fee_percent are still used.
        response.update(
            fee_fixed=round(asset.deposit_fee_fixed, asset.significant_decimals),
            fee_percent=asset.deposit_fee_percent,
        )

    if "extra_info" in integration_response:
        response["extra_info"] = integration_response["extra_info"]
        if not isinstance(response["extra_info"], dict):
            logger.error("Invalid 'extra_info' returned from process_sep6_request()")
            raise ValueError()

    return response, 200


def parse_request_args(request: Request) -> Dict:
    asset = Asset.objects.filter(
        code=request.GET.get("asset_code"), sep6_enabled=True, deposit_enabled=True
    ).first()
    if not asset:
        return {"error": render_error_response(_("invalid 'asset_code'"))}

    lang = request.GET.get("lang")
    if lang:
        err_resp = validate_language(lang)
        if err_resp:
            return {"error": err_resp}
        activate_lang_for_request(lang)

    memo_type = request.GET.get("memo_type")
    if memo_type and memo_type not in Transaction.MEMO_TYPES:
        return {"error": render_error_response(_("invalid 'memo_type'"))}

    try:
        make_memo(request.GET.get("memo"), memo_type)
    except (ValueError, MemoInvalidException):
        return {"error": render_error_response(_("invalid 'memo' for 'memo_type'"))}

    args = {
        "asset": asset,
        "memo_type": memo_type,
        "memo": request.GET.get("memo"),
        "lang": lang,
        "type": request.GET.get("type"),
        **extract_sep9_fields(request.GET),
    }

    # add remaining extra params, it's on the anchor to validate them
    for param, value in request.GET.items():
        if param not in args:
            args[param] = value

    return args
