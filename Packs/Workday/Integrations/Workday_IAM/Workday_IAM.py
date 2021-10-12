import demistomock as demisto  # noqa: F401
from CommonServerPython import *  # noqa: F401

''' IMPORTS '''
import traceback
import requests
import re
import dateparser

USERNAME_FIELD = 'username'
EMAIL_ADDRESS_FIELD = 'email'
EMPLOYEE_ID_FIELD = 'employeeid'
IS_PROCESSED_FIELD = 'isprocessed'
FORCE_SYNC_FIELD = 'forcesync'
DISPLAY_NAME_FIELD = 'displayname'
LAST_DAY_OF_WORK_FIELD = 'lastdayofwork'
TERMINATION_DATE_FIELD = 'terminationdate'
TERMINATION_TRIGGER_FIELD = 'terminationtrigger'
EMPLOYMENT_STATUS_FIELD = 'employmentstatus'
PREHIRE_FLAG_FIELD = 'prehireflag'
REHIRED_EMPLOYEE_FIELD = 'rehiredemployee'
HIRE_DATE_FIELD = 'hiredate'
AD_ACCOUNT_STATUS_FIELD = 'adaccountstatus'
OLD_USER_DATA_FIELD = 'olduserdata'
MERGED_USER_PROFILE_FIELD = 'mergeduserprofile'
SOURCE_PRIORITY_FIELD = 'sourcepriority'
SOURCE_OF_TRUTH_FIELD = 'sourceoftruth'
CONVERSION_HIRE_FIELD = 'conversionhire'
USER_PROFILE_INC_FIELD = 'UserProfile'
USER_PROFILE_INDICATOR = 'User Profile'

DATE_FORMAT = "%m/%d/%Y"
READ_TIME_OUT_IN_SECONDS = 300
DEFAULT_MAPPER_IN = 'IAM Sync User - Workday'
BATCH_SIZE = 2000

NEW_HIRE_EVENT_TYPE = 'IAM - New Hire'
UPDATE_USER_EVENT_TYPE = 'IAM - Update User'
REHIRE_USER_EVENT_TYPE = 'IAM - Rehire User'
TERMINATE_USER_EVENT_TYPE = 'IAM - Terminate User'
ACTIVATE_AD_EVENT_TYPE = 'IAM - AD User Activation'
DEACTIVATE_AD_EVENT_TYPE = 'IAM - AD User Deactivation'
DEFAULT_INCIDENT_TYPE = 'IAM - Sync User'


# Disable insecure warnings
requests.packages.urllib3.disable_warnings()


class Client(BaseClient):
    """
    Client will implement the service API, should not contain Cortex XSOAR logic.
    Should do requests and return data
    """
    # Getting Workday Full User Report with a given report URL. This uses RaaS
    def get_full_report(self, report_url):
        res = self._http_request(method="GET", full_url=report_url, url_suffix="",
                                 timeout=READ_TIME_OUT_IN_SECONDS)
        return res.get('Report_Entry', [])


''' HELPER FUNCTIONS '''


def report_to_indicators(report_entries, mapper_in, workday_date_format, deactivation_date_field,
                         days_before_hire_to_sync, days_before_hire_to_enable_ad, source_priority):
    indicators = []
    email_to_user_profile: Dict[str, Dict] = {}

    for entry in report_entries:
        workday_user = get_workday_user_from_entry(entry, mapper_in, workday_date_format, source_priority)
        if is_report_missing_required_user_data(workday_user) \
                or not has_reached_threshold_date(days_before_hire_to_sync, workday_user) \
                or new_hire_email_already_taken(workday_user, None, email_to_user_profile) \
                or is_termination_event(workday_user, None, deactivation_date_field, first_run=True):
            continue

        indicator = workday_user_to_indicator(workday_user, days_before_hire_to_enable_ad)
        indicators.append(indicator)
        email_to_user_profile[workday_user.get(EMAIL_ADDRESS_FIELD)] = indicator

    return indicators


def workday_user_to_indicator(workday_user, days_before_hire_to_enable_ad):
    if not has_reached_threshold_date(days_before_hire_to_enable_ad, workday_user):
        workday_user[AD_ACCOUNT_STATUS_FIELD] = 'Pending'

    raw_json = workday_user.copy()
    raw_json['value'] = workday_user.get(EMAIL_ADDRESS_FIELD)
    raw_json['type'] = USER_PROFILE_INDICATOR
    indicator = {
        'value': workday_user.get(EMAIL_ADDRESS_FIELD),
        'type': USER_PROFILE_INDICATOR,
        'rawJSON': raw_json,
        'fields': workday_user
    }
    return indicator


def convert_incident_fields_to_cli_names(data):
    converted_data = {}
    for k, v in data.items():
        key_machine_name = re.sub(r'[^a-zA-Z0-9]', '', k).lower()
        converted_data[key_machine_name] = v
    return converted_data


def reformat_date_fields(user_profile, workday_date_format):
    for date_field in [TERMINATION_DATE_FIELD, LAST_DAY_OF_WORK_FIELD, HIRE_DATE_FIELD]:
        if user_profile.get(date_field):
            datetime_obj = datetime.strptime(user_profile[date_field], workday_date_format)
            user_profile[date_field] = datetime_obj.strftime(DATE_FORMAT)


def get_workday_user_from_entry(entry, mapper_in, workday_date_format, source_priority):
    workday_user = demisto.mapObject(entry, mapper_in, DEFAULT_INCIDENT_TYPE)
    workday_user = convert_incident_fields_to_cli_names(workday_user)
    reformat_date_fields(workday_user, workday_date_format)

    workday_user[SOURCE_PRIORITY_FIELD] = source_priority
    workday_user[SOURCE_OF_TRUTH_FIELD] = 'Workday IAM'

    return workday_user


def has_reached_threshold_date(num_of_days_before_hire, workday_user):
    if not num_of_days_before_hire and num_of_days_before_hire != 0:
        return True

    hire_date = dateparser.parse(workday_user.get(HIRE_DATE_FIELD)).date()
    today = datetime.today().date()
    delta = (hire_date - today).days
    if delta > num_of_days_before_hire:
        demisto.debug(f'Skipped creating an incident for user '
                      f'with email address {workday_user.get(EMAIL_ADDRESS_FIELD)} - '
                      f'the hire date of the employee is in more than {num_of_days_before_hire} days.')
        return False
    return True


def new_hire_email_already_taken(workday_user, demisto_user, email_to_user_profile):
    if demisto_user is None and email_to_user_profile.get(workday_user.get(EMAIL_ADDRESS_FIELD)) is not None:
        demisto.debug(f'Skipped creating a user profile for the following employee:\n{workday_user}\n\n'
                      f'The user profile doesn\'t exist but its email is already being used by another user.')
        return True
    return False


def is_report_missing_required_user_data(workday_user):
    if not workday_user.get(EMAIL_ADDRESS_FIELD) or not workday_user.get(EMPLOYEE_ID_FIELD) \
            or not workday_user.get(HIRE_DATE_FIELD):
        demisto.debug(f'Skipped creating an incident for the following user profile:\n{workday_user}\n\n'
                      f'The user profile does not contain email address/employee ID/hire date, '
                      f'to fix please add the missing data to the report.')
        return True
    return False


def is_tufe_user(demisto_user):
    if demisto_user is not None and demisto_user.get(TERMINATION_TRIGGER_FIELD) == 'TUFE':
        demisto.debug(f'Dropping event for user with email {demisto_user.get(EMAIL_ADDRESS_FIELD)} '
                      f'as it is a TUFE user.')
        return True
    return False


def is_event_processed(demisto_user):
    if demisto_user is not None and demisto_user.get(IS_PROCESSED_FIELD) is True:
        demisto.debug(f'Dropping event for user with email {demisto_user.get(EMAIL_ADDRESS_FIELD)} '
                      f'as it is currently being processed.')
        return True
    return False


def is_termination_event(workday_user, demisto_user, deactivation_date_field, first_run=False):
    if not first_run and (demisto_user is None or demisto_user.get(AD_ACCOUNT_STATUS_FIELD, '') == 'Disabled'):
        # skipping termination check - user does not exist or already terminated
        return False

    prehire_flag = workday_user.get(PREHIRE_FLAG_FIELD, '').lower() == 'true'
    employment_status = workday_user.get(EMPLOYMENT_STATUS_FIELD, '').lower()

    if (deactivation_date := workday_user.get(deactivation_date_field)):
        deactivation_date = dateparser.parse(workday_user.get(deactivation_date_field))
    today = datetime.today()

    if (employment_status == 'terminated' and prehire_flag is False) \
            or (deactivation_date and deactivation_date <= today):
        demisto.debug(f'A termination event was detected for user '
                      f'with email address {workday_user.get(EMAIL_ADDRESS_FIELD)}.')
        return True

    return False


def is_display_name_already_taken(workday_user, display_name_to_user_profile):
    user_display_name = workday_user.get(DISPLAY_NAME_FIELD)
    demisto_users_by_display_name = display_name_to_user_profile.get(user_display_name)
    if demisto_users_by_display_name is None:
        return None

    demisto.debug(f'Detected an IAM - New Hire event for user with email address '
                  f'{workday_user.get(EMAIL_ADDRESS_FIELD)}, but its display name is already taken. '
                  f'Please review the incident.')

    existing_user = demisto_users_by_display_name[0]
    merged_user_profile = workday_user.copy()
    merged_user_profile.update({
        EMAIL_ADDRESS_FIELD: existing_user.get(EMAIL_ADDRESS_FIELD),
        USERNAME_FIELD: existing_user.get(USERNAME_FIELD)
    })
    return merged_user_profile


def is_new_hire_event(demisto_user, workday_user, deactivation_date_field):
    if demisto_user is not None:
        return False

    prehire_flag = workday_user.get(PREHIRE_FLAG_FIELD, '').lower() == 'true'
    employment_status = workday_user.get(EMPLOYMENT_STATUS_FIELD, '')

    if prehire_flag is True and not employment_status:
        demisto.debug(f'A new hire event was detected for user '
                      f'with email address {workday_user.get(EMAIL_ADDRESS_FIELD)}.')
        return True

    if (deactivation_date := workday_user.get(deactivation_date_field)):
        deactivation_date = dateparser.parse(workday_user.get(deactivation_date_field))
    today = datetime.today()

    if employment_status.lower() != 'terminated' and (not deactivation_date or today <= deactivation_date):
        demisto.debug(f'A non-terminated user with an email address {workday_user.get(EMAIL_ADDRESS_FIELD)} '
                      f'was not found in XSOAR, even though a pre-hire was not detected. Syncing anyway.')
        return True

    return False


def is_rehire_event(demisto_user, workday_user, changed_fields, deactivation_date_field):
    if demisto_user is None or demisto_user.get(AD_ACCOUNT_STATUS_FIELD, '') != 'Disabled':
        # skipping rehire check - user does not exist or already active/pending
        return False
    prehire_flag = workday_user.get(PREHIRE_FLAG_FIELD, '').lower() == 'true'
    is_rehired_employee = workday_user.get(REHIRED_EMPLOYEE_FIELD, '').lower() == 'yes'

    if (deactivation_date := workday_user.get(deactivation_date_field)):
        deactivation_date = dateparser.parse(workday_user.get(deactivation_date_field))
    today = datetime.today()

    if prehire_flag is True and is_rehired_employee and changed_fields \
            or (deactivation_date_field in changed_fields \
                and (not deactivation_date or deactivation_date > today)):
        demisto.debug(f'A rehire event was detected for user '
                      f'with email address {workday_user.get(EMAIL_ADDRESS_FIELD)}.')
        return True
    return False


def is_ad_activation_event(demisto_user, workday_user, days_before_hire_to_enable_ad):
    if demisto_user and demisto_user.get(AD_ACCOUNT_STATUS_FIELD, '') == 'Pending':
        if has_reached_threshold_date(days_before_hire_to_enable_ad, workday_user):
            demisto.debug(f'An Active Directory activation event was detected for user '
                          f'with email address {workday_user.get(EMAIL_ADDRESS_FIELD)}.')
            return True
    return False


def is_ad_deactivation_event(demisto_user, workday_user, days_before_hire_to_enable_ad, source_priority):
    """
    Checks whether the event is IAM - Deactivate User in Active Directory.
    Note:
    To avoid misdetection of deactivation events for conversion hires, we check that:
    1. The current SOURCE_PRIORITY_FIELD is Workday's - otherwise it's a conversion hire in its first fetch.
    2. CONVERSION_HIRE_FIELD is not True - otherwise it's a conversion hire.

    Args:
        demisto_user: The user profile in XSOAR.
        workday_user: Workday user in XSOAR format.
        days_before_hire_to_enable_ad: Number of days before hire date to enable Active Directory account,
                                        `None` if should sync instantly.
        source_priority: Source priority level.

    Returns:
        (bool). True iff the event is an AD deactivation.
    """
    if not demisto_user \
            or demisto_user.get(SOURCE_PRIORITY_FIELD) != source_priority \
            or demisto_user.get(CONVERSION_HIRE_FIELD) is True:
        return False

    if demisto_user.get(AD_ACCOUNT_STATUS_FIELD, '') == 'Enabled':
        if not has_reached_threshold_date(days_before_hire_to_enable_ad, workday_user):
            demisto.debug(f'An Active Directory deactivation event was detected for user '
                          f'with email address {workday_user.get(EMAIL_ADDRESS_FIELD)}.')
            return True
    return False


def is_update_event(workday_user, changed_fields):
    if changed_fields and workday_user.get(EMPLOYMENT_STATUS_FIELD, '').lower() != 'terminated':
        demisto.debug(f'An update event was detected for user '
                      f'with email address {workday_user.get(EMAIL_ADDRESS_FIELD)}.')
        return True
    return False


def should_force_sync(demisto_user):
    if demisto_user and demisto_user.get(FORCE_SYNC_FIELD, '').lower() == 'true':
        demisto.debug(f'Forcing user sync for user '
                      f'with email address {demisto_user.get(EMAIL_ADDRESS_FIELD)}.')

def get_all_user_profiles():
    query = f'type:\"{USER_PROFILE_INDICATOR}\"'
    display_name_to_user_profile: Dict[str, List[Dict]] = {}
    employee_id_to_user_profile: Dict[str, Dict] = {}
    email_to_user_profile: Dict[str, Dict] = {}

    def handle_batch(user_profiles):
        for user_profile in user_profiles:
            user_profile = user_profile.get('CustomFields', {})
            display_name = user_profile.get(DISPLAY_NAME_FIELD)
            employee_id = user_profile.get(EMPLOYEE_ID_FIELD)
            email = user_profile.get(EMAIL_ADDRESS_FIELD)
            display_name_to_user_profile.setdefault(display_name, []).append(user_profile)
            employee_id_to_user_profile[employee_id] = user_profile
            email_to_user_profile[email] = user_profile

    search_indicators = IndicatorsSearcher()

    query_result = search_indicators.search_indicators_by_version(query=query, size=BATCH_SIZE)
    while query_result.get('iocs', []):
        handle_batch(query_result.get('iocs', []))
        query_result = search_indicators.search_indicators_by_version(query=query, size=BATCH_SIZE)

    return display_name_to_user_profile, employee_id_to_user_profile, email_to_user_profile


def get_demisto_user(email_to_user_profile, employee_id_to_user_profile, workday_user):
    demisto_user = None
    if (employee_id := workday_user.get(EMPLOYEE_ID_FIELD)):
        demisto_user = employee_id_to_user_profile.get(employee_id)
    if not demisto_user:
        if (email := workday_user.get(EMAIL_ADDRESS_FIELD)):
            demisto_user = email_to_user_profile.get(email)

    return demisto_user


def get_orphan_users_events(user_emails, source_priority):
    """ Gets all users that don't exist in the Workday report anymore and terminate them in XSOAR. """
    _, _, email_to_user_profile = get_all_user_profiles()
    events = []
    orphan_users = [email for email, user in email_to_user_profile.items()
                    if email not in user_emails and user.get(EMPLOYMENT_STATUS_FIELD) != 'Terminated'
                    and user.get(SOURCE_PRIORITY_FIELD) == source_priority
                    and user.get(IS_PROCESSED_FIELD) is False]

    if orphan_users:
        demisto.debug(f'Found orphan users: {orphan_users}')
        for email in orphan_users:
            email_to_user_profile[email][EMPLOYMENT_STATUS_FIELD] = 'Terminated'
            entry = {
                'Email_Address': email,
                USER_PROFILE_INC_FIELD: email_to_user_profile[email],
                'Emp_ID': email_to_user_profile[email].get(EMPLOYEE_ID_FIELD),
                TERMINATION_TRIGGER_FIELD: 'Orphan'
            }
            event = {
                'name': email,
                'rawJSON': json.dumps(entry),
                'type': TERMINATE_USER_EVENT_TYPE,
                'details': 'An orphan user was detected (could not find the user in Workday report). '
                           'Please review and terminate if necessary.'
            }

            events.append(event)

    return events


def get_profile_changed_fields(demisto_user, workday_user):
    if not demisto_user:
        return None, None
    changed_fields = {}

    for field, workday_value in workday_user.items():
        if (workday_value and not demisto_user.get(field)) or workday_value != demisto_user.get(field):
            changed_fields[field] = workday_value

    return changed_fields


def is_valid_source_of_truth(demisto_user, source_priority):
    if demisto_user is not None \
            and demisto_user.get(SOURCE_PRIORITY_FIELD, 1) < source_priority:
        demisto.debug(f'Skipped creating an incident for user profile {demisto_user.get(EMAIL_ADDRESS_FIELD)}: '
                      f'The user profile in XSOAR has a higher source priority level.')
        return False
    return True


def get_event_details(entry, workday_user, demisto_user, days_before_hire_to_sync, days_before_hire_to_enable_ad,
                      deactivation_date_field, display_name_to_user_profile, email_to_user_profile,
                      employee_id_to_user_profile, source_priority):
    """
    This function detects the event type and creates a dictionary which holds the event details.
    If the event should not be created, None is returned.

    Args:
        entry: The employee's report entry.
        workday_user: Workday user in XSOAR format.
        demisto_user: The user profile in XSOAR.
        deactivation_date_field: Deactivation date field - "lastdayofwork" or "terminationdate".
        days_before_hire_to_sync: Number of days before hire date to sync hires, -1 if should sync instantly.
        days_before_hire_to_enable_ad: Number of days before hire date to enable Active Directory account,
                                        -1 if should sync instantly.
        display_name_to_user_profile: A dictionary that maps display names to user profile indicators in XSOAR.
        email_to_user_profile: A dictionary that maps email addresses to user profile indicators in XSOAR.
        employee_id_to_user_profile: A dictionary that maps employee ids to user profile indicators in XSOAR.
        source_priority: The source priority number.

    Returns:
        event: The event details.
    """
    user_email = workday_user.get(EMAIL_ADDRESS_FIELD)
    changed_fields = get_profile_changed_fields(demisto_user, workday_user)
    demisto.debug(f'{changed_fields=}')

    if not has_reached_threshold_date(days_before_hire_to_sync, workday_user) \
            or new_hire_email_already_taken(workday_user, demisto_user, email_to_user_profile) \
            or is_report_missing_required_user_data(workday_user) \
            or not is_valid_source_of_truth(demisto_user, source_priority) \
            or is_event_processed(demisto_user):
        return None

    if is_new_hire_event(demisto_user, workday_user, deactivation_date_field):
        event_type = NEW_HIRE_EVENT_TYPE
        event_details = 'The user has been hired.'

        if merged_user_profile := is_display_name_already_taken(workday_user, display_name_to_user_profile):
            workday_user[MERGED_USER_PROFILE_FIELD] = merged_user_profile
            event_type = DEFAULT_INCIDENT_TYPE
            event_details = 'Detected an IAM - New Hire event, but display name already exists. Please review.'

    elif is_ad_activation_event(demisto_user, workday_user, days_before_hire_to_enable_ad):
        event_type = ACTIVATE_AD_EVENT_TYPE
        event_details = 'Active Directory user account was enabled.'

    elif is_ad_deactivation_event(demisto_user, workday_user, days_before_hire_to_enable_ad, source_priority):
        event_type = DEACTIVATE_AD_EVENT_TYPE
        event_details = 'Active Directory user account was disabled due to hire date postponement.'

    elif is_rehire_event(demisto_user, workday_user, changed_fields, deactivation_date_field):
        event_type = REHIRE_USER_EVENT_TYPE
        event_details = 'The user has been rehired.'

    elif is_termination_event(workday_user, demisto_user, deactivation_date_field):
        event_type = TERMINATE_USER_EVENT_TYPE
        event_details = 'The user has been terminated.'

    elif is_update_event(workday_user, changed_fields):
        event_type = UPDATE_USER_EVENT_TYPE
        changed_fields_str = '\n'.join([f'{k} field was updated to "{v}".' for k, v in changed_fields.items()])
        event_details = f'The user has been updated:\n{changed_fields_str}'
        workday_user[OLD_USER_DATA_FIELD] = demisto_user

        if demisto_user.get(SOURCE_PRIORITY_FIELD) != source_priority:
            workday_user[CONVERSION_HIRE_FIELD] = True
            event_details += '\n\nNote: a conversion hire was detected.'

    elif should_force_sync(demisto_user):
        event_type = UPDATE_USER_EVENT_TYPE
        event_details = 'Forced User Sync'
        workday_user[OLD_USER_DATA_FIELD] = demisto_user
        demisto_user[FORCE_SYNC_FIELD] = 'false'

    else:
        demisto.debug(f'Could not detect changes in report for user with email address {user_email} - skipping.')
        return None

    if is_tufe_user(demisto_user) and event_type != REHIRE_USER_EVENT_TYPE:
        return None

    entry[USER_PROFILE_INC_FIELD] = {**(demisto_user or {}), **workday_user}

    return {
        'name': user_email,
        'rawJSON': json.dumps(entry),
        'type': event_type,
        'details': event_details
    }


def process_report_entries(mapper_in, workday_date_format, deactivation_date_field,
                           days_before_hire_to_sync, days_before_hire_to_enable_ad,
                           source_priority, report_entries, fetch_limit,
                           number_of_entries_to_process, user_emails):
    """
    Iterates over the given report entries and processes them into XSOAR IAM events.

    Args:
        mapper_in: Incoming mapper's name
        workday_date_format: Date format in Workday report.
        deactivation_date_field: Deactivation date field - "lastdayofwork" or "terminationdate".
        days_before_hire_to_sync: Number of days before hire date to sync hires, `None` if should sync instantly.
        days_before_hire_to_enable_ad: Number of days before hire date to enable Active Directory account,
                                        `None` if should sync instantly.
        source_priority: Source priority level.
        report_entries: Unproccessed report entries.
        fetch_limit: Maximal number of events to retrieve.
        user_emails: An aggregated list of the user emails in the Workday report. Used to detect orphan users.

    Returns:
        events: Incidents/Events that will be created in Cortex XSOAR
    """
    events = []
    number_of_processed_entries = 0
    demisto.debug('WORKDAY: entered process_report_entries function')
    try:
        demisto.debug('WORKDAY: before getting all user profiles')
        display_name_to_user_profile, employee_id_to_user_profile, email_to_user_profile = get_all_user_profiles()
        demisto.debug('WORKDAY: after getting all user profiles')

        demisto.debug('Starting to process Workday report entries')
        for entry in report_entries:
            number_of_processed_entries += 1
            # get the user event (if exists) according to workday report
            workday_user = get_workday_user_from_entry(entry, mapper_in, workday_date_format, source_priority)
            demisto_user = get_demisto_user(email_to_user_profile, employee_id_to_user_profile, workday_user)

            user_email = workday_user.get(EMAIL_ADDRESS_FIELD)
            demisto.debug(f'Getting event details for user with email address {user_email}.\n'
                          f'Current user data in XSOAR: {demisto_user=}\nData in Workday: {workday_user=}')
            user_emails.append(user_email)

            event = get_event_details(entry, workday_user, demisto_user, days_before_hire_to_sync,
                                      days_before_hire_to_enable_ad, deactivation_date_field,
                                      display_name_to_user_profile, email_to_user_profile,
                                      employee_id_to_user_profile, source_priority)
            if event is not None:
                events.append(event)

            if len(events) == fetch_limit or number_of_processed_entries == number_of_entries_to_process - 1:
                break

        demisto.debug(f'Processed {number_of_processed_entries} Workday entries.')

        if not events:
            demisto.info('Did not detect any changes in the current Workday report entries batch.')

    except Exception as e:
        demisto.error('Failed to fetch events. Reason: ' + str(e))
        raise e
    unprocessed_report_entries = report_entries[number_of_processed_entries:]

    return events, unprocessed_report_entries


''' INTEGRATION COMMANDS '''


def fetch_samples(client, mapper_in, report_url, workday_date_format):
    """
    This function returns a list of (at most) five sample events (used for classification and mapping only).

    Args:
        client: Workday client
        mapper_in: Incoming mapper's name
        report_url: The report full URL
        workday_date_format: Date format in Workday

    Returns:
        events: Incidents/events that will be used as samples for classification and mapping.
    """
    events = []
    num_of_samples = 5
    try:
        report_entries = client.get_full_report(report_url)
        num_of_samples = min(num_of_samples, len(report_entries))
        report_entries = report_entries[:num_of_samples]

        for entry in report_entries:
            workday_user = demisto.mapObject(entry, mapper_in, DEFAULT_INCIDENT_TYPE)
            workday_user = convert_incident_fields_to_cli_names(workday_user)
            reformat_date_fields(workday_user, workday_date_format)

            entry[USER_PROFILE_INC_FIELD] = workday_user
            event = {
                "name": workday_user.get(EMAIL_ADDRESS_FIELD),
                "rawJSON": json.dumps(entry),
                "details": 'This is a sample event.'
            }
            events.append(event)
    except Exception as e:
        demisto.error('Failed to fetch events. Reason: ' + str(e))
        raise e

    return events


def get_full_report_command(client, mapper_in, report_url, workday_date_format, source_priority):
    report_entries = client.get_full_report(report_url)
    outputs = [get_workday_user_from_entry(entry, mapper_in, workday_date_format, source_priority)
               for entry in report_entries]
    results = CommandResults(
        outputs_prefix='WorkdayIAM.ReportEntry',
        outputs_key_field=EMPLOYEE_ID_FIELD,
        outputs=outputs
    )
    return results


def fetch_incidents(client, mapper_in, report_url, workday_date_format, deactivation_date_field,
                    days_before_hire_to_sync, days_before_hire_to_enable_ad, source_priority,
                    last_run, fetch_limit, processed_entries_percentage_per_fetch):
    """
    This function will execute each interval (default is 1 minute).

    Args:
        client: Workday client
        mapper_in: Incoming mapper's name
        report_url: The report full URL.
        workday_date_format: Date format in Workday report.
        deactivation_date_field: Deactivation date field - "lastdayofwork" or "terminationdate".
        days_before_hire_to_sync: Number of days before hire date to sync hires, `None` if should sync instantly.
        days_before_hire_to_enable_ad: Number of days before hire date to enable Active Directory account,
                                       `None` if should sync instantly.
        source_priority: Source priority level.
        last_run: A Dictionary containing information from the previous fetch run.
        fetch_limit: Maximal number of events to retrieve.
        processed_entries_percentage_per_fetch: Percentage of entries to process per fetch.

    Returns:
        events: Incidents/Events that will be created in Cortex XSOAR
        last_run: A Dictionary containing information from the current fetch run.
    """
    if not last_run.get('report_entries', []):
        if user_emails := last_run.get('user_emails', []):
            last_run['orphan_users_events'] = get_orphan_users_events(user_emails, source_priority)

        if orphan_user_events := last_run.get('orphan_users_events'):
            last_run['orphan_users_events'] = orphan_user_events[fetch_limit:]
            return orphan_user_events[:fetch_limit], last_run

        demisto.debug('WORKDAY: before getting full workday report')
        report_entries = client.get_full_report(report_url)
        demisto.debug('WORKDAY: after getting full workday report')

        last_run.update({
            'report_entries': report_entries,
            'number_of_entries_to_process': len(report_entries) * processed_entries_percentage_per_fetch,
            'user_emails': []
        })

    events, unprocessed_report_entries = process_report_entries(
        mapper_in,
        workday_date_format,
        deactivation_date_field,
        days_before_hire_to_sync,
        days_before_hire_to_enable_ad,
        source_priority,
        report_entries=last_run.get('report_entries', []),
        fetch_limit=fetch_limit,
        number_of_entries_to_process=last_run.get('number_of_entries_to_process', BATCH_SIZE),
        user_emails=last_run.get('user_emails', [])
    )

    last_run.update({
        'synced_users': True,
        'report_entries': unprocessed_report_entries
    })

    return events, last_run


def workday_first_run_command(client, mapper_in, report_url, workday_date_format, deactivation_date_field,
                              days_before_hire_to_sync, days_before_hire_to_enable_ad, source_priority):
    report_entries = client.get_full_report(report_url)
    indicators = report_to_indicators(report_entries, mapper_in, workday_date_format, deactivation_date_field,
                                      days_before_hire_to_sync, days_before_hire_to_enable_ad, source_priority)
    for b in batch(indicators, batch_size=BATCH_SIZE):
        demisto.createIndicators(b)

    return [], {'synced_users': True}


def test_module(client, is_fetch, report_url, mapper_in, workday_date_format,
                days_before_hire_to_sync, days_before_hire_to_enable_ad, source_priority):
    """
    Returning 'ok' indicates that the integration works like it is supposed to. Connection to the service is successful.
    Anything else will fail the test.
    """
    # test API connectivity
    client.get_full_report(report_url)

    if is_fetch:
        fetch_incidents(
            client=client,
            mapper_in=mapper_in,
            report_url=report_url,
            workday_date_format=workday_date_format,
            days_before_hire_to_sync=days_before_hire_to_sync,
            days_before_hire_to_enable_ad=days_before_hire_to_enable_ad,
            deactivation_date_field=TERMINATION_DATE_FIELD,
            source_priority=source_priority,
            last_run={},
            fetch_limit=1,
            processed_entries_percentage_per_fetch=50,
            fetch_orphans_mode=False
        )

    return 'ok'


def main():
    command = demisto.command()
    params = demisto.params()

    is_fetch = params.get('isFetch')
    report_url = params.get('report_url')
    verify_certificate = not params.get('insecure', False)
    proxy = params.get('proxy', False)
    mapper_in = params.get('mapper_in', DEFAULT_MAPPER_IN)
    workday_username = params.get('credentials', {}).get('identifier')
    workday_password = params.get('credentials', {}).get('password')
    workday_date_format = params.get('workday_date_format', DATE_FORMAT)
    deactivation_date_field = params.get('deactivation_date_field').lower().replace('_', '')
    source_priority = int(params.get('source_priority', '1'))
    fetch_limit = int(params.get('max_fetch', '50'))
    processed_entries_percentage_per_fetch = float(params.get('processed_entries_percentage', '50')) / 100

    days_before_hire_to_sync = params.get('days_before_hire_to_sync')
    if days_before_hire_to_sync:
        days_before_hire_to_sync = int(days_before_hire_to_sync)

    days_before_hire_to_enable_ad = params.get('days_before_hire_to_enable_ad')
    if days_before_hire_to_enable_ad:
        days_before_hire_to_enable_ad = int(days_before_hire_to_enable_ad)

    demisto.debug(f'Command being called is {command}')

    client = Client(
        base_url='',  # using report_url in _http_request
        verify=verify_certificate,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        },
        proxy=proxy,
        ok_codes=(200, 204),
        auth=requests.auth.HTTPBasicAuth(workday_username, workday_password)
    )

    try:
        if command == 'test-module':
            return_results(test_module(client, is_fetch, report_url, mapper_in, workday_date_format,
                                       days_before_hire_to_sync, days_before_hire_to_enable_ad, source_priority))

        if command == 'workday-iam-get-full-report':
            return_results(get_full_report_command(client, mapper_in, report_url, workday_date_format, source_priority))

        if command == 'fetch-incidents':
            '''
                Checks if there are events are stored in the integration context.
                If yes, it gets it from there. Else, it makes a call to Workday to get a new report
                Returns the first x events (x being the fetch limit) and stores the remaining in integration context
            '''
            last_run = demisto.getLastRun()
            report_url = params.get('report_url')

            if params.get('fetch_samples') and not last_run.get('fetched_samples'):
                sample_events = fetch_samples(
                    client=client,
                    mapper_in=mapper_in,
                    report_url=report_url,
                    workday_date_format=workday_date_format
                )
                demisto.incidents(sample_events)
                demisto.setLastRun({'fetched_samples': True})

            else:
                if not last_run.get('synced_users') and params.get('first_run'):
                    events, last_run = workday_first_run_command(
                        client=client,
                        mapper_in=mapper_in,
                        report_url=report_url,
                        workday_date_format=workday_date_format,
                        deactivation_date_field=deactivation_date_field,
                        days_before_hire_to_sync=days_before_hire_to_sync,
                        days_before_hire_to_enable_ad=days_before_hire_to_enable_ad,
                        source_priority=source_priority
                    )
                else:
                    # Get the events from Workday by making an API call. Last run is updated only when API call is made
                    events, last_run = fetch_incidents(
                        client=client,
                        mapper_in=mapper_in,
                        report_url=report_url,
                        workday_date_format=workday_date_format,
                        deactivation_date_field=deactivation_date_field,
                        days_before_hire_to_sync=days_before_hire_to_sync,
                        days_before_hire_to_enable_ad=days_before_hire_to_enable_ad,
                        source_priority=source_priority,
                        last_run=last_run,
                        fetch_limit=fetch_limit,
                        processed_entries_percentage_per_fetch=processed_entries_percentage_per_fetch,
                    )

                demisto.incidents(events)
                demisto.setLastRun(last_run)

    except Exception as e:
        return_error(f'Failed to execute {demisto.command()} command, Error: {e}. Traceback: {traceback.format_exc()}')


if __name__ in ['__main__', 'builtin', 'builtins']:
    main()
