'''
The event loop for the index runner.
'''

import json
import logging
import os
import requests
import time
import traceback

from logging import Logger
from typing import Callable, Dict, Any

from confluent_kafka import Consumer, KafkaError

from src.utils.config import config
from src.utils.service_utils import wait_for_dependencies

Message = Dict[str, Any]

# TODO TEST unit tests


def start_loop(
        consumer: Consumer,
        message_handler: Callable[[Message], None],
        on_success: Callable[[Message], None] = lambda msg: None,
        on_failure: Callable[[Message, Exception], None] = lambda msg, e: None,
        on_ready: Callable[[], None] = lambda: None,
        on_config_update: Callable[[], None] = lambda: None,
        logger: Logger = logging.getLogger('src.event_loop')):
    """
    Run the indexer event loop.

    :param consumer: A Kafka consumer which will be polled for messages.
    :param message_handler: a processor for messages from Kafka.
    :param on_success: called after message_handler has returned sucessfully and the message
        offset has been committed to Kafka. A noop by default.
    :param on_failure: called if the message_handler, the Kafka commit, or on_success throws an
        exception. A noop by default.
    :param on_ready: called when the event loop is intialized and about to start.
    :param on_config_update: called when the configuration has been updated.
    :param logger: a logger to use for logging events. By default a standard logger for
        'src.event_loop'.
    """
    # Remove the ready indicator file if it has been written on a previous boot
    if os.path.exists(config()['proc_ready_path']):
        os.remove(config()['proc_ready_path'])
    # Wait for dependency services (ES and RE) to be live
    wait_for_dependencies(timeout=180)
    # Used for re-fetching the configuration with a throttle
    last_updated_minute = int(time.time() / 60)
    if not config()['global_config_url']:
        config_tag = _fetch_latest_config_tag()
    on_ready()
    # Touch a temp file indicating the daemon is ready
    with open(config()['proc_ready_path'], 'w') as fd:
        fd.write('')

    while True:
        msg = consumer.poll(timeout=0.5)
        if msg is None:
            continue
        curr_min = int(time.time() / 60)
        if not config()['global_config_url'] and curr_min > last_updated_minute:
            # Check for configuration updates
            latest_config_tag = _fetch_latest_config_tag()
            last_updated_minute = curr_min
            if config_tag is not None and latest_config_tag != config_tag:
                config(force_reload=True)
                config_tag = latest_config_tag
                on_config_update()
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                logger.info('End of stream.')
            else:
                logger.error(f"Kafka message error: {msg.error()}")
            continue
        val = msg.value().decode('utf-8')
        try:
            msg = json.loads(val)
        except ValueError as err:
            logger.error(f'JSON parsing error: {err}')
            logger.error(f'Message content: {val}')
            consumer.commit()
            continue
        logger.info(f'Received event: {msg}')
        start = time.time()
        try:
            message_handler(msg)
            # Move the offset for our partition
            consumer.commit()
            on_success(msg)
            logger.info(f"Handled {msg['evtype']} message in {time.time() - start}s")
        except Exception as err:
            logger.error(f'Error processing message: {err.__class__.__name__} {err}')
            logger.error(traceback.format_exc())
            # Save this error and message to a topic in Elasticsearch
            on_failure(msg, err)


# might make sense to move this into the config module
def _fetch_latest_config_tag():
    """
    Using the Github release API, check for a new version of the config.
    https://developer.github.com/v3/repos/releases/
    """
    github_release_url = config()['github_release_url']
    if config()['github_token']:
        headers = {'Authorization': f"token {config()['github_token']}"}
    else:
        headers = {}
    try:
        resp = requests.get(url=github_release_url, headers=headers)
    except Exception as err:
        logging.error(f"Unable to fetch indexer config from github: {err}")
        # Ignore any error and continue; try the fetch again later
        return None
    if not resp.ok:
        logging.error(f"Unable to fetch indexer config from github: {resp.text}")
        return None
    data = resp.json()
    return data['tag_name']