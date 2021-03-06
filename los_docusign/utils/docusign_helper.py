import json
import logging
import re
from datetime import datetime, timedelta
from os import path
from xml.etree import cElementTree as ElementTree

from django.conf import settings
from django.http import Http404

# import sentry_sdk
from django.utils import timezone
from docusign_esign import ApiClient
from docusign_esign.client.api_exception import ApiException

from los_docusign.models import (
    DocusignEnvelopeAuditLog,
    DocusignEnvelopeStageData,
    DocusignOrgTemplate,
    DocuSignUserAuth,
)

from .XmlParser import XmlDictConfig

SCOPES = ["signature"]

LOGGER = logging.getLogger("root")


def get_docusign_user(organization_pk):
    try:
        # Try if the user is the available and has the docusign account
        docusign_user = DocuSignUserAuth.objects.get(organization_pk=organization_pk)
    except DocuSignUserAuth.DoesNotExist:
        # Else use the admin user
        # Tejas, isn't default_user supposed to be the default user we use as a
        # fallback?
        docusign_user = DocuSignUserAuth.objects.get(default_user=True)

    return docusign_user


def get_access_token(docusign_user):
    docusign_token_expiry = settings.DOCUSIGN_TOKEN_EXPIRY_IN_SECONDS

    if docusign_user.expires_at >= timezone.now():
        access_token = docusign_user.access_token
    else:
        token_response = _jwt_auth(docusign_user.docusign_api_username)
        access_token = token_response.access_token
        docusign_user.access_token = access_token
        docusign_user.expires_at = timezone.now() + timedelta(
            seconds=int(docusign_token_expiry)
        )
        docusign_user.save()

    return access_token


def check_docusign_access_token(organization_pk):
    docusign_user = get_docusign_user(organization_pk)
    token_response = _jwt_auth(docusign_user.docusign_api_username)
    if not token_response:
        use_scopes = SCOPES
        if "impersonation" not in use_scopes:
            use_scopes.append("impersonation")
        consent_scopes = " ".join(use_scopes)
        redirect_uri = settings.DOCUSIGN_REDIRECT_APP_URL
        consent_url = (
            f"https://{settings.DOCUSIGN_AUTHORIZATION_SERVER}/oauth/auth?response_type=code&"
            f"scope={consent_scopes}&client_id={settings.DOCUSIGN_CLIENT_ID}&redirect_uri={redirect_uri}"
        )
        return consent_url
    return None


def _jwt_auth(docusign_api_username):
    """JSON Web Token authorization"""
    api_client = ApiClient()
    api_client.set_base_path(settings.DOCUSIGN_AUTHORIZATION_SERVER)
    use_scopes = SCOPES
    if "impersonation" not in use_scopes:
        use_scopes.append("impersonation")

    # Catch IO error
    try:
        private_key = _get_private_key().encode("ascii").decode("utf-8")

    except (OSError, IOError) as err:
        # sentry_sdk.capture_exception(Exception(f'OSError, IOError in Docusign JWT Auth'))
        return "error"

    try:
        jwtTokenResponse = api_client.request_jwt_user_token(
            client_id=str(settings.DOCUSIGN_CLIENT_ID),
            user_id=docusign_api_username,
            oauth_host_name=str(settings.DOCUSIGN_AUTHORIZATION_SERVER),
            private_key_bytes=private_key,
            expires_in=3600,
            scopes=use_scopes,
        )
    except ApiException as err:

        body = err.body.decode("utf8")
        # Grand explicit consent for the application
        if "consent_required" in body:
            return None
        else:
            LOGGER.error(f"Error while getting the jwt token for docusign: {err}")
            raise Exception

    return jwtTokenResponse


def _get_private_key():
    """
    Check that the private key present in the file and if it is, get it from the file.
    In the opposite way get it from config variable.
    """
    private_key_file = path.abspath(settings.DOCUSIGN_PRIVATE_KEY_FILE)
    if path.isfile(private_key_file):
        with open(private_key_file) as private_key_file:
            private_key = private_key_file.read()
    else:
        private_key = settings.DOCUSIGN_PRIVATE_KEY_FILE.encode().decode(
            "unicode-escape"
        )

    return private_key


def populate_text_tabs(text_tabs_forms, text_tabs_data: dict):
    # Need to populate all the text tabs with the values
    for textTabsInfo in text_tabs_forms:
        tab_label = textTabsInfo["tabLabel"]
        try:
            textTabsInfo["value"] = text_tabs_data.get(tab_label)
        except KeyError as e:
            print(f"Key not found {e}")


def get_docusign_template(organization_pk, template_name=None):
    docusign_payload = None
    try:
        docusign_template = DocusignOrgTemplate.objects.get(
            organization_model="organization",
            docusign_template__template_type=template_name,
            organization_pk=organization_pk,
        ).docusign_template
    except DocusignOrgTemplate.DoesNotExist:
        dsua = DocuSignUserAuth.objects.get(default_user=True)
        docusign_template = DocusignOrgTemplate.objects.get(
            object_pk=dsua.object_pk, docusign_template__template_type=template_name
        ).docusign_template

    docusign_payload = docusign_template.docusign_payload
    if docusign_payload is None:
        print("Payload Not found for org. Check database..return")
        LOGGER.error(
            f"Payload Not found for org {organization_pk}. Check database..return"
        )
        return

    # resp = json.loads(docusign_payload)
    return docusign_payload


def process_docusign_webhook(xml_string):
    # request_data_dict = request.data
    root = ElementTree.XML(xml_string)
    request_data_dict = XmlDictConfig(root)
    m = re.search(
        "{http://www.docusign.net/API/(.+?)}EnvelopeStatus", str(request_data_dict)
    )
    api_version = None
    if m:
        api_version = m.group(1)
    else:
        # sentry_sdk.capture_exception(Exception(f'Failed to retrieve API Version for the DocuSign Webhook: {str(request_data_dict)}'))
        raise Http404

    docusign_schema = "{http://www.docusign.net/API/" + api_version + "}"

    # Since we are not using any of the data sent back by DocuSign, we clear those fields which potentially causes json.dumps to fail to parse Decimal Values which are set by the Parser
    request_data_dict[f"{docusign_schema}EnvelopeStatus"][
        f"{docusign_schema}RecipientStatuses"
    ][f"{docusign_schema}RecipientStatus"][f"{docusign_schema}TabStatuses"] = None
    request_data_dict[f"{docusign_schema}EnvelopeStatus"][
        f"{docusign_schema}RecipientStatuses"
    ][f"{docusign_schema}RecipientStatus"][f"{docusign_schema}UserName"] = None
    request_data_dict[f"{docusign_schema}EnvelopeStatus"][
        f"{docusign_schema}RecipientStatuses"
    ][f"{docusign_schema}RecipientStatus"][f"{docusign_schema}FormData"] = None
    line = re.sub(
        r"({http://www.docusign.net/API/[0-9].[0-9]})",
        "",
        json.dumps(request_data_dict),
    )
    docusign_data_dict = json.loads(line)
    envelopeId = docusign_data_dict["EnvelopeStatus"]["EnvelopeID"]

    try:
        DocusignEnvelopeStageData.objects.get(envelope_id=envelopeId)
    except DocusignEnvelopeStageData.DoesNotExist:
        LOGGER.error(f"Envelope id  {envelopeId} not found in system")
        raise Exception(f"Envelope id  {envelopeId} not found in system")

    return _extract_envelope_status(docusign_data_dict)


def _extract_envelope_status(docusign_data_dict):
    print("processing docusign")

    envelopeId = docusign_data_dict["EnvelopeStatus"]["EnvelopeID"]
    recipient_status_tag = docusign_data_dict["EnvelopeStatus"]["RecipientStatuses"][
        "RecipientStatus"
    ]

    recipient_status = recipient_status_tag.get("Status", None)

    recipient_auth_status = recipient_status_tag.get(
        "RecipientAuthenticationStatus", None
    )
    recipient_id_question_status = None
    recipient_id_lookup_status = None

    if recipient_auth_status:
        recipient_idquestion_result = recipient_auth_status.get(
            "IDQuestionsResult", None
        )
        if recipient_idquestion_result:
            recipient_id_question_status = recipient_idquestion_result["Status"]
        recipient_id_lookup_result = recipient_auth_status.get("IDLookupResult", None)
        if recipient_id_lookup_result:
            recipient_id_lookup_status = recipient_id_lookup_result["Status"]

    envelope_status = docusign_data_dict["EnvelopeStatus"]["Status"]

    try:
        envelope_stage_data = DocusignEnvelopeStageData.objects.get(
            envelope_id=envelopeId
        )
    except DocusignEnvelopeStageData.DoesNotExist:
        LOGGER.error(
            f"Envelope id  {envelopeId} not found in system while extracting status from Webhook notification"
        )
        return
    except Exception as e:
        LOGGER.error(
            f"Exception while extracting status from Webhook notification: {e}"
        )
        return

    recipient_status = str(recipient_status).lower()
    envelope_status = str(envelope_status).lower()
    if recipient_auth_status:
        recipient_id_question_status = str(recipient_id_question_status).lower()
        recipient_id_lookup_status = str(recipient_id_lookup_status).lower()

    # Let's not overwrite the status of authentication failed if the recipient failed authentication.
    # We need this to know if the receipient failed authentication and later on completed the application
    if not envelope_stage_data.recipient_status == "authentication failed":
        envelope_stage_data.recipient_status = recipient_status

    if "failed" in (recipient_id_lookup_status, recipient_id_question_status):
        envelope_stage_data.recipient_status = "authentication failed"
        envelope_stage_data.recipient_auth_info = recipient_auth_status
        recipient_status = "authentication failed"

    event_value = envelope_status

    if recipient_status == "authentication failed" and envelope_status == "sent":
        event_value = "authentication failed"

    # TODO: Need to understand how can we log this in the DocuSignEnvelopeAuditLog, since we do not have the content type?
    log = DocusignEnvelopeAuditLog(
        content_type=envelope_stage_data.content_type,
        object_pk=envelope_stage_data.object_pk,
        event_received_at=datetime.now(),
        envelope_id=envelope_stage_data.envelope_id,
        event_type="WEBHOOK",
        event_value=event_value,
        remote_addr="0.0.0.0",
    )
    log.save()

    # If the user fails KBA, then move the status back to APPLICANT Correction.
    if event_value == "authentication failed":
        envelope_status = "authentication failed"

    envelope_stage_data.envelope_status = envelope_status
    envelope_stage_data.updated_at = timezone.now()
    envelope_stage_data.save()

    print(f"END process_docusign_notification: {envelopeId}")
    LOGGER.debug(f"END process_docusign_notification: {envelopeId}")
    return envelopeId, envelope_status
