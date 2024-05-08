from django.shortcuts import render, redirect
import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import requests
import xml.etree.ElementTree as ET
from .models import MuseumObject, SyncLock
from datetime import datetime
from django.utils import timezone
import time
from requests.auth import HTTPBasicAuth
import logging
import os
import environ
from decouple import config

collection_types_variable = config(
    'COLLECTION_TYPES', default="VanabbeCollectie")
collection_types = collection_types_variable.split(" ")

plone_username = config("PLONE_USERNAME")
plone_password = config("PLONE_PASSWORD")

logger = logging.getLogger('collection_sync')
sync_start_logger = logging.getLogger('sync_start_logger')
create_update_object_logger = logging.getLogger('create_update_object_logger')


def index(request):
    museum_objects = MuseumObject.objects.all()
    sync_running = SyncLock.objects.get(id=1).is_locked
    print(museum_objects)
    return render(request, "collection/index.html", {
        "museum_objects": museum_objects,
        "sync_running": sync_running,
    })


def sync_status(request):
    sync = SyncLock.objects.get(id=1)
    return JsonResponse({'is_locked': sync.is_locked})


def not_synced_objects(request):
    # Retrieve objects that are not synced
    sync_running = SyncLock.objects.get(id=1).is_locked
    not_synced = MuseumObject.objects.filter(synced=False)
    return render(request, "collection/index.html", {
        "museum_objects": not_synced,
        "sync_running": sync_running
    })


def check_status(request, ccObjectID):
    sync_running = SyncLock.objects.get(id=1).is_locked
    object = MuseumObject.objects.filter(ccObjectID=ccObjectID)
    return render(request, "collection/index.html", {
        "museum_objects": object,
        "sync_running": sync_running
    })


def all_objects(request):
    sync_running = SyncLock.objects.get(id=1).is_locked
    all_objects = MuseumObject.objects.all()
    return render(request, "collection/index.html", {
        "museum_objects": all_objects,
        "sync_running": sync_running,
    })


def delete_plone_dates(request):
    museum_objects = MuseumObject.objects.all()
    for museum_object in museum_objects:
        sync = SyncLock.objects.get(id=1)
        if sync.stop_requested:
            print("Sync stopped by user.")
            break
        if museum_object.plone_timestamp is not None:
            museum_object.plone_timestamp = None
            museum_object.save()
        else:
            continue
    return JsonResponse({'message': 'Dates are cleaned!'})


def stopsync(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        if data.get('action') == 'stop':
            sync = SyncLock.objects.get(id=1)
            sync.stop_requested = True
            sync.save()
            end_time = timezone.now()
            logger.info(f"Sync process stopped at {end_time}")
            return JsonResponse({'message': 'Sync stop requested.'})
        else:
            return JsonResponse({'message': 'Invalid action!'}, status=400)
    return JsonResponse({'message': 'Only POST requests are allowed!'}, status=405)


def syncadlib(request):
    sync, _ = SyncLock.objects.get_or_create(id=1)
    sync_running = sync.is_locked
    if request.method == 'POST':
        data = json.loads(request.body)
        if data.get('action') == 'run':
            # Server-side function to execute
            if sync_running == False:
                sync_start()
            else:
                return JsonResponse({'message': 'Sync is already running!'})
            return JsonResponse({'message': 'Server function executed successfully!'})
        else:
            return JsonResponse({'message': 'Invalid action!'}, status=400)
    return JsonResponse({'message': 'Only POST requests are allowed!'}, status=405)


def manualsync(request, ccObjectID, ccIndexName):
    sync, _ = SyncLock.objects.get_or_create(id=1)
    sync_running = sync.is_locked

    if request.method == 'POST':
        data = json.loads(request.body)
        if data.get('action') == 'run':
            if not sync_running:
                sync_one_plone_object(ccObjectID, ccIndexName)
                return JsonResponse({'message': f'Synced object {ccObjectID} successfully!'})
            else:
                return JsonResponse({'message': 'Sync is already running!'}, status=409)
        else:
            return JsonResponse({'message': 'Invalid action!'}, status=400)
    return JsonResponse({'message': 'Only POST requests are allowed!'}, status=405)


def sync_start():
    sync, _ = SyncLock.objects.get_or_create(id=1)
    sync.is_locked = True
    sync.stop_requested = False  # Reset the stop flag before starting
    sync.save()

    start_time = timezone.now()
    print('triggered')
    print(f'collection types: {collection_types}')
    logger.info(f"Sync process started at {start_time}")
    sync_start_logger.info(f"Sync process started at {start_time}")
    create_update_object_logger.info(f"Sync process started at {start_time}")
    try:
        for collection in collection_types:
            if sync.stop_requested:
                stopped_time = datetime.now()
                sync_start_logger.info(
                    f"User requested stop at {stopped_time}")
                break
            sync_database(collection)

        for collection in collection_types:
            if sync.stop_requested:
                stopped_time = datetime.now()
                sync_start_logger.info(
                    f"User requested stop at {stopped_time}")
                break
            sync_plone(collection)
    finally:
        sync.is_locked = False
        sync.stop_requested = False
        sync.save()
        end_time = timezone.now()
        logger.info(f"Sync process finished at {end_time}")
        sync_start_logger.info(f"Sync process finished at {end_time}")
        create_update_object_logger.info(
            f"Sync process finished at {end_time}")


def sync_database(collection):
    count = get_total_count(collection)
    print(count)
    for offset in range(0, 50, 10):
        sync = SyncLock.objects.get(id=1)
        if sync.stop_requested:
            print("Sync stopped by user during database sync.")
            stopped_time = timezone.now()
            sync_start_logger.info(
                f"Sync process stopped by the user at {stopped_time}")
            break
        records = fetch_xml_data(collection, offset)
        for record in records:
            dc_record = record.find(".//dc_record")
            if not dc_record:
                continue

            index_name = dc_record.findtext(".//ccIndexName")
            ccObjectID = dc_record.findtext(".//ccObjectID")
            timestamp = dc_record.findtext(".//timestamp")
            if timestamp:
                timestamp_parsed = datetime.strptime(
                    timestamp, "%d-%m-%Y %H:%M")
                timestamp_parsed = timezone.make_aware(timestamp_parsed)
                title = "Untitled"
                if index_name == 'VanAbbeCollectie':
                    title = dc_record.findtext(".//objectTitle")
                elif index_name == "VanabbeTentoonstellingen":
                    title = dc_record.findtext(".//eventTitle")
                elif index_name == "VanAbbeBibliotheek":
                    title = dc_record.findtext(".//BookTitle")

                museum_object, created = MuseumObject.objects.get_or_create(
                    ccObjectID=ccObjectID,
                    index_name=index_name,
                    defaults={'title': title,
                              'api_lastmodified': timestamp_parsed, }
                )

                if not created:
                    print(f"Updated: {ccObjectID}")
                else:
                    print(f"Created new object: {ccObjectID}")
            else:
                create_update_object_logger.error(
                    f"Plone object creation failed")

    return "finished"


def fetch_xml_data(collection, offset):
    """
    Fetches XML data from the predefined API endpoint.

    Returns:
    - str: The fetched XML data.
    """
    api_url = f"http://62.221.199.184:17718/action=get&command=search&query=ccIndexName={collection}&fields=ccObjectID,timestamp, ccIndexName,objectTitle,eventTitle,BookTitle&range={offset}-{offset+10}"
    print(f"api_url: {api_url}")
    response = requests.get(api_url)
    response.raise_for_status()
    api_answer = response.text
    root = ET.fromstring(api_answer)
    records = root.findall(".//record")
    print('return records')
    return records


def get_total_count(collection):
    api_url = f"http://62.221.199.184:17718/action=get&command=search&query=ccIndexName={collection}&fields=ccObjectId"
    response = requests.get(api_url)
    response.raise_for_status()
    api_answer = response.text
    root = ET.fromstring(api_answer)
    count = root.findtext(".//request/count")
    return count


def sync_plone(collection):
    sync = SyncLock.objects.get(id=1)
    museum_objects = MuseumObject.objects.filter(index_name=collection)
    for museum_object in museum_objects:
        sync = SyncLock.objects.get(id=1)
        if sync.stop_requested:
            print("Sync stopped by user.")
            stopped_time = datetime.now()
            sync_start_logger.info(
                f"User requested stop at {stopped_time}")
            break
        if museum_object.plone_timestamp is None:
            time.sleep(2)
            create_update_object(museum_object)
        elif museum_object.api_lastmodified > museum_object.plone_timestamp:
            create_update_object(museum_object)
        else:
            continue


def sync_one_plone_object(ccObjectID, index_name):
    museum_object = MuseumObject.objects.get(
        ccObjectID=ccObjectID, index_name=index_name)
    create_update_object(museum_object)


def create_update_object(museum_object):

    plone_url = f"http://localhost:8080/Plone/nl/@@admin_fixes?op=import_collection_object&object_id={museum_object.ccObjectID}"
    try:
        response = requests.get(plone_url, auth=HTTPBasicAuth(
            plone_username, plone_password))
        if response.status_code == 200:
            logger.info(
                f"Plone object created/updated successfully for ccObjectID: {museum_object.ccObjectID}")
            create_update_object_logger.info(
                f"Plone object created/updated successfully for ccObjectID: {museum_object.ccObjectID}")

            print("Plone object created successfully")

        else:
            print("Failed to create Plone object: ", response.status_code)
            logger.error(
                f"Failed to create/update Plone object for ccObjectID: {museum_object.ccObjectID}. Status code: {response.status_code}")
            create_update_object_logger.error(
                f"Failed to create/update Plone object for ccObjectID: {museum_object.ccObjectID}. Status code: {response.status_code}")

    except requests.exceptions.RequestException as e:
        print("Network exception occurred: ", e)
        logger.exception(
            f"Network exception occurred for ccObjectID: {museum_object.ccObjectID}: {e}")

    # Update the timestamp only if the request is successful
    if response.status_code == 200:
        museum_object.synced = True
        museum_object.plone_timestamp = museum_object.api_lastmodified
        museum_object.save()