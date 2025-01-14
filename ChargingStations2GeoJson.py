#!/Users/roseren/Documents/CItcomAI/wp1_data_collection/citcomAI_venv/bin/python

import argparse
import time
import json
import shutil
import datetime
import re
import requests
import logging
import os
from pymongo import MongoClient
from pymongo.server_api import ServerApi
import sys
import math
import xml.etree.cElementTree as ET
from utils.GeoJsonBuilder import GeoJsonBuilder
from collections import OrderedDict
from database_credentials import * 


ns = {}
ch = logging.StreamHandler(sys.stdout)
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
strict_mode = False

# MongoDB credentials
MONGO_DB_NAME = 'Chargy_stations'
MONGO_DB_COLLECTION_NAME = 'EV_stations'
URI = 'mongodb+srv://{}:{}@cluster0.c3ofuvi.mongodb.net/?retryWrites=true&w=majority'.format(MONGO_DB_USERNAME,MONGO_DB_PASSWORD)


def is_valid_file(parser, arg):
    if not os.path.exists(arg):
        parser.error("The file %s does not exist!" % arg)
    return arg


def get_namespace(element):
    match = re.match(r"\{(.*?)\}", element.tag)
    if match:
        return match.group(1)
    raise ValueError("Fatal: Couldn't identify the namespace!")


def process_charging_device(charging_point, station_name):
    json_device = json.loads(charging_point.text)
    sum_connectors = json_device["numberOfConnectors"]
    charging_point_id = json_device["id"]
    charging_point_name = json_device["name"].strip()

    if re.search(r"\s", charging_point_name):
        logger.warning("Charging point name '%s' probably contains some text, make sure to verify this before importing. (Belongs to charging station: '%s')" % (
            charging_point_name, station_name))
    if not charging_point_name.startswith("CP"):
        logger.warning("Charging point name '%s' does not have a chargy Id (CPabcd), make sure to verify this before importing. (Belong to charging station: '%s')" %
                       (charging_point_name, station_name))

    # Check contents of the charging points
    charging_points_status = 0
    charging_speeds_for_points = []
    for chargingPoint in json_device["connectors"]:
        charging_point_description = chargingPoint["description"].strip()

        if charging_point_description.upper() != "OFFLINE":
            charging_points_status += 1

        charging_speeds_for_points.append(math.floor(chargingPoint["maxchspeed"]))
        #TODO: Fix
        #if math.floor(chargingPoint["maxchspeed"]) != output_wattage_value:
        #    logger.error("Power Output mismatch for '%s', was expecting '%s' and got '%s'." % (
        #        charging_point_name, output_wattage_value, chargingPoint["maxchspeed"]))
        #    if strict_mode:
        #        raise ValueError("Power Output Error!")
    
    count_connectors_not_offline = charging_points_status
    return sum_connectors, charging_point_id, charging_point_name, count_connectors_not_offline, charging_speeds_for_points


def process_charging_station(station):
    properties = {}
    station_name = ' '.join(station.find("ns:name", ns).text.split())
    
    if not (station_name.startswith("Chargy Ok") or station_name.startswith("SuperChargy Ok")):
        properties["operator"] = "Chargy"
    
    visibility = int(station.find("ns:visibility", ns).text)
    if visibility != 1:
        logger.warning("Node '%s', visibility flag != 1." % station_name)
        properties["operational_status"] = "closed"

    properties["amenity"] = "charging_station"
    properties['name'] = station_name
    properties["brand"] = "Chargy"
    properties["opening_hours"] = "24/7"
    properties["motorcar"] = "yes"
    properties["phone"] = "+352 80062020"
    properties["authentication:membership_card"] = "yes"

    # Each charging station can have multiple charging points
    charging_devices = station.findall(
        "ns:ExtendedData/ns:Data[@name='chargingdevice']/ns:value", ns)

    properties["devices"] = len(charging_devices)
    if(len(charging_devices) > 1):
        logger.info("Charging Station '%s' contains '%s' charging points, tagging as 1 charging station." % (
            station_name, len(charging_devices)))

    # Process all the charging points
    sum_connectors = 0
    count_connectors_not_offline = 0
    output_wattage_for_all_stations = []
    for device in charging_devices:
        r_sum_connectors, r_charging_point_id, charging_point_name, r_cnt_connectors_offline, output_wattages = process_charging_device(
            device, station_name)
        sum_connectors += r_sum_connectors
        count_connectors_not_offline += r_cnt_connectors_offline
        output_wattage_for_all_stations.extend(output_wattages)

    if count_connectors_not_offline == 0:
        logger.warning(
            "Charging station '%s' is OFFLINE (All sockets are OFFLINE)" % station_name)
        #properties["operational_status"] = "closed"
    
    properties["socket:type2:output"] = ";".join(list(map(lambda wattage: "%s kW" % wattage, sorted(set(output_wattage_for_all_stations))))) 

    countChargingPoints = int(station.find(
        "ns:ExtendedData/ns:Data[@name='CPnum']/ns:value", ns).text)

    if countChargingPoints != sum_connectors:
        logger.error("Charging point count mismatch for '%s'. Total reported count is %s, summed description count is %s." % (
            charging_point_name, countChargingPoints, sum_connectors))
        if strict_mode:
            raise ValueError("Charging Point Count mismatch!")

    properties["socket:type2"] = countChargingPoints
    properties["capacity"] = countChargingPoints

    lon, lat = station.find("ns:Point/ns:coordinates", ns).text.split(",")

    return GeoJsonBuilder.create_feature(properties, float(lon), float(lat))

def download_data_from_opendata_portal():
    logger.debug("File not provided, downloading from data.public.lu...")
    downloaded_file_result = requests.get("https://data.public.lu/fr/datasets/r/22f9d77a-5138-4b02-b315-15f306b77034")
    dataset_filename = "chargy_%s.kml" % time.strftime("%Y%m%d_%H%M%S")
    dataset_full_path = "data_cache/%s" % dataset_filename
    logger.debug("Saving to %s" % dataset_full_path)
    if not os.path.exists(os.path.dirname(dataset_full_path)):
        os.makedirs(os.path.dirname(dataset_full_path))
    with open(dataset_full_path, "wb") as f:
        f.write(downloaded_file_result.content)
    return dataset_full_path

def extract_data_from_kml(input_file, output_file):
    if input_file is None:
        input_file = download_data_from_opendata_portal()
    
    logger.debug("Reading File: %s" % input_file)
    doc = ET.parse(input_file)
    root = doc.getroot()

    ns["ns"] = "%s" % get_namespace(root)
    stations = root.findall(".//ns:Placemark", ns)
    logger.debug("Found %s stations" % len(stations))

    features = []
    for station in stations:
        computed_feature = process_charging_station(station)
        if computed_feature is not None:
            features.append(computed_feature)

    export_artifact = GeoJsonBuilder.create_geojson(features)
    if os.path.dirname(output_file) and not os.path.exists(os.path.dirname(output_file)):
        os.makedirs(os.path.dirname(output_file))

    with open(output_file, "w") as outfile:
        logger.debug("Writing to: %s" % output_file)
        json.dump(export_artifact, outfile)
    

    logger.debug("Success! Output file contains %s points." % len(features))


# load kml files 
output_filename = "results/charging_stations_{}.geojson".format(time.strftime("%Y%m%d_%H%M%S"))
extract_data_from_kml(input_file=None,output_file=output_filename)

doc = ET.parse('data_cache/chargy_20230808_111911.kml')
root = doc.getroot()
stations = root.findall(".//ns:Placemark", ns)
features = []

for station in stations:
    properties = {}
    station_name = ' '.join(station.find("ns:name", ns).text.split())
    charging_devices = station.findall("ns:ExtendedData/ns:Data[@name='chargingdevice']/ns:value", ns)
    print(station_name)
    print(charging_devices)

test_station = stations[1]
charging_points = test_station.findall("ns:ExtendedData/ns:Data[@name='chargingdevice']/ns:value",ns)
n_chargin_point = len(charging_points)
n_connectors = int(test_station.find("ns:ExtendedData/ns:Data[@name='CPnum']/ns:value", ns).text)



for station in stations:
        computed_feature = process_charging_station(station)
        if computed_feature is not None:
            features.append(computed_feature)



def store_to_db():
    try:
        json_filename = os.listdir('results/')[0] 
        f = open(os.path.join('results/',json_filename))
        data = json.load(f)
        #define datetime to the nearest minute 
        now = datetime.datetime.now()
        now = now - datetime.timedelta(minutes = now.minute % 1,
                                       seconds = now.second,
                                       microseconds=now.microsecond)
        data['date_time'] = str(now)

        # dump json in MongoDb
        client = MongoClient(URI,server_api = ServerApi('1'))
        db = client[MONGO_DB_NAME]
        collection = db[MONGO_DB_COLLECTION_NAME]
        collection.insert_one(data)
        client.close()
        print('Data fetched and stored successfully')

    
    except Exception as e:
        print(' An error occured: {}'.format(str(e)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert the Chargy KML Dataset into GeoJSON Points")
    parser.add_argument("infile", metavar="INFILE", nargs="?",
                        default=None,
                        help="KML File from Chargy. If unset, the most recent file will be pulled from the OpenData Portal",
                        type=lambda x: is_valid_file(parser, x))

    parser.add_argument("-o", "--outfile", metavar="OUTFILE", nargs="?",
                        default="results/charging_stations_%s.geojson" % time.strftime(
                            "%Y%m%d_%H%M%S"),
                        help="Overrides the default filename for the exported GeoJSON file")

    parser.add_argument("-v", "--verbose", action="store_const", dest="loglevel",
                        help="Overrides the default LogLevel", const=logging.DEBUG)

    parser.add_argument("-s", "--strict", action="store_const", dest="strict_mode",
                        help="Enables strict mode. Halt execution if any unexpected value is found.", default=strict_mode, const=True)

    args = parser.parse_args()

    if args.loglevel is not None:
        logger.setLevel(args.loglevel)
    
    strict_mode = args.strict_mode

    extract_data_from_kml(args.infile, args.outfile)
    store_to_db()
    # clean directories and files to save space
    shutil.rmtree('data_cache')
    shutil.rmtree('results')
    print('temporary data folders deleted')

