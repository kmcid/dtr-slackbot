# pip3 install python-dotenv flask slackclient slackeventsapi apscheduler psycopg2-binary

import os
import json
import uuid
import slack
import datetime
import psycopg2
from waitress import serve
from dotenv import load_dotenv
from flask import Flask, request, Response
from pathlib import Path
from slackeventsapi import SlackEventAdapter
from apscheduler.schedulers.background import BackgroundScheduler

# environment variables loader
env_path = Path(".") / ".env"
load_dotenv(dotenv_path=env_path)

app = Flask(__name__)
slack_event_adapter = SlackEventAdapter(
    os.environ["SIGNING_SECRET"], "/slack/events", app
)
client = slack.WebClient(token=os.environ["SLACK_TOKEN"])

# create database connection, commit changes automatically
connection = psycopg2.connect(
    dbname=os.environ["DB_NAME"],
    user=os.environ["DB_USERNAME"],
    password=os.environ["DB_PASSWORD"],
    host=os.environ["DB_HOST"],
)
connection.autocommit = True

# check existance of database tables
cursor = connection.cursor()
cursor.execute(
    "select exists (select from information_schema.tables where table_schema='public' and table_name='records')"
)
records_exists = cursor.fetchone()
cursor.close()

# create database tables if not exists
if records_exists[0] == False:
    cursor = connection.cursor()
    cursor.execute(
        "create table records (id uuid primary key default uuid_generate_v4(), user_id text, arrival timestamp, departure timestamp, leave text, bot_departure boolean)"
    )
    cursor.close()

# declared variables
BOT_ID = client.api_call("auth.test")["user_id"]
EXPECTED_WORK_HOURS = 60 * 0.5  # 60 seconds x 60 minutes
DTR_RECORDS = {}


# setup dtr record persist and cleanup
def persist_and_cleanup_dtr_record():
    print("running dtr cleanup")
    global DTR_RECORDS

    cleanup_departure_time = datetime.datetime.now()
    for key, record in DTR_RECORDS.items():
        id = record.get("id")
        user_id = record.get("user_id")

        if "departure" not in record:
            cursor = connection.cursor()
            cursor.execute(
                f"insert into records (id, user_id, departure, bot_departure) values ('{id}', '{user_id}', '{cleanup_departure_time}', True) on conflict(id) do update set (user_id, departure, bot_departure) = ('{user_id}', '{cleanup_departure_time}', True) returning id"
            )
            cursor.close()

    DTR_RECORDS = {}


# initialise and start background scheduler
scheduler = BackgroundScheduler()
# scheduler.add_job(persist_and_cleanup_dtr_record, "cron", hour=6)
scheduler.add_job(persist_and_cleanup_dtr_record, "cron", minute="*/2")
scheduler.start()


# message task for slack rest notification, runs on a different thread
def take_rest_notification(*args):
    client.chat_postEphemeral(
        channel=args[0],
        user=args[1],
        text=f"Hi <@{args[1]}> you have worked for 8 hours, you should take rest and logout.",
    )


# returns user's dtr record or generates a new one
def get_user_dtr_record(user_id):
    # create dtr record for user if not yet available
    if user_id not in DTR_RECORDS:
        DTR_RECORDS[user_id] = {}

    if "user_id" not in DTR_RECORDS[user_id]:
        DTR_RECORDS[user_id]["user_id"] = user_id

    # set id of user's dtr record
    if "id" not in DTR_RECORDS[user_id]:
        DTR_RECORDS[user_id]["id"] = uuid.uuid4()

    return DTR_RECORDS[user_id]


# tells if user has arrived based on dtr record
def user_has_arrived(record):
    return True if "arrival" in record else False


# tells if user has departed based on dtr record
def user_has_departed(record):
    return True if "departure" in record else False


# removes scheduled jobs in records by job name
def cleanup_user_job(job_name, record):
    if f"{job_name}" in record:
        existing_job = scheduler.get_job(record[job_name])
        if existing_job is not None:
            scheduler.remove_job(record[job_name])


@app.route("/dtr/arrival", methods=["POST"])
def dtr_arrival():
    data = request.form
    user_id = data.get("user_id")
    channel_id = data.get("channel_id")
    text = data.get("text") or "in"

    record = get_user_dtr_record(user_id=user_id)
    re_arrival = user_has_arrived(record=record)

    if "departure" in record:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=f"Hi <@{user_id}>, we already recorded your departure, so timing in is unavailable. Records will reset at 6 am, try again later.",
        )
        return Response(), 200

    user_arrival = datetime.datetime.now()
    expected_out = user_arrival + datetime.timedelta(seconds=EXPECTED_WORK_HOURS)
    record["arrival"] = user_arrival

    cleanup_user_job(job_name="take_rest_job", record=record)

    take_rest_job = scheduler.add_job(
        take_rest_notification,
        trigger="date",
        next_run_time=expected_out,
        args=(channel_id, user_id),
    )
    record["take_rest_job"] = take_rest_job.id

    cursor = connection.cursor()
    cursor.execute(
        f"insert into records (id, user_id, arrival) values ('{record['id']}', '{user_id}', '{user_arrival}') on conflict(id) do update set (user_id, arrival) = ('{user_id}', '{user_arrival}') returning id"
    )
    cursor.close()

    client.chat_postMessage(
        channel=channel_id,
        text=f"<@{user_id}> timed in{' again' if re_arrival else ''} @ {user_arrival}: {text}",
    )
    return Response(), 200


@app.route("/dtr/departure", methods=["POST"])
def dtr_departure():
    data = request.form
    user_id = data.get("user_id")
    channel_id = data.get("channel_id")
    text = data.get("text") or "out"

    record = get_user_dtr_record(user_id=user_id)
    re_departed = user_has_departed(record=record)

    if "arrival" not in record:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=f"Hi <@{user_id}>, you do not have a recorded arrival time, you should time in instead.",
        )
        return Response(), 200

    user_departure = datetime.datetime.now()
    record["departure"] = user_departure

    cleanup_user_job(job_name="take_rest_job", record=record)

    cursor = connection.cursor()
    cursor.execute(
        f"insert into records (id, user_id, departure) values ('{record['id']}', '{user_id}', '{user_departure}') on conflict(id) do update set (user_id, departure) = ('{user_id}', '{user_departure}') returning id"
    )
    cursor.close()

    client.chat_postMessage(
        channel=channel_id,
        text=f"<@{user_id}> timed out{' again' if re_departed else ''} @ {user_departure}: {text}",
    )
    return Response(), 200


@app.route("/dtr/record", methods=["POST"])
def dtr_record():
    data = request.form
    user_id = data.get("user_id")
    channel_id = data.get("channel_id")

    current_time = datetime.datetime.now()

    user_record = get_user_dtr_record(user_id=user_id)
    is_online = False or user_has_arrived(record=user_record)
    if user_has_departed(record=user_record):
        is_online = False

    leave_text, online_text, offline_text = "", "", ""
    for key, record in DTR_RECORDS.items():
        print(record)
        if "leave" in record:
            leave_text += f"<@{key}> is vacation leave!\n"
        elif "departure" in record:
            offline_text += f"<@{key}> is offline (logout @ {record['departure']}, elapsed {record['departure'] - record['arrival']})\n"
        elif "arrival" in record:
            online_text += f"<@{key}> is online (login @ {record['arrival']}, elapsed {current_time - record['arrival']})\n"

    client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        blocks=json.dumps(
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Hello <@{user_id}> you have not signed in yet.\nHere is your request to view current DTR record\n\n *Leave:*"
                        if not user_record.get("arrival")
                        else f"Hello <@{user_id}> you are currently {'online' if is_online else 'offline'} ({'login' if is_online else 'logout'} @ {user_record['arrival'] if is_online else user_record['departure']}, elapsed {current_time - user_record['arrival'] if is_online else user_record['departure'] - user_record['arrival']})\nHere is your request to view current DTR record\n\n *Leave:*",
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{leave_text or 'No users are on leave today'}\n\n\n\n *Online:*",
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{online_text or 'No online users right now'}\n\n\n\n *Offline:*",
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{offline_text or 'No offline users right now'}\n",
                    },
                },
            ]
        ),
    )
    return Response(), 200


@app.route("/dtr/dayoff", methods=["POST"])
def dtr_dayoff():
    data = request.form
    user_id = data.get("user_id")
    channel_id = data.get("channel_id")

    client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        blocks=json.dumps(
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Hello <@{user_id}>\nSelect which dates are you taking off and what type of leave",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "datepicker",
                            "initial_date": "2023-05-24",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Select start date of your dayoff",
                            },
                            "action_id": "dayoff_start",
                        },
                        {
                            "type": "datepicker",
                            "initial_date": "2023-05-24",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Select end date of your dayoff",
                            },
                            "action_id": "dayoff_end",
                        },
                    ],
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "static_select",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Select what type of day off",
                            },
                            "options": [
                                {
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Philippine Annual",
                                    },
                                    "value": "PHILIPPINE_ANNUAL",
                                }
                            ],
                            "action_id": "dayoff_type",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Submit day off"},
                            "value": "dayoff_save",
                            "action_id": "dayoff_save",
                        },
                    ],
                },
            ]
        ),
    )
    return Response(), 200


if __name__ == "__main__":
    if os.environ["ENVIRONMENT"] == "PRODUCTION":
        serve(app, host="0.0.0.0", port=8080)
    else:
        app.run(host='0.0.0.0', debug=True, use_reloader=True)
