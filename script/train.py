# coding : utf-8

import csv
import math
import os
import pickle
import sys
import xgboost as xgb

from collections import defaultdict
from datetime import datetime
from scipy.sparse import csr_matrix

from config import Config
from logger import LOGGER
from mall_shop_map import MallShopMap
from mall_wifi_map import MallWifiMap

def LoadFeatures(file_path):
    assert os.path.isfile(file_path)
    with open(file_path, 'rb') as fin:
        data = pickle.load(fin)
        indices = pickle.load(fin)
        indptr = pickle.load(fin)
        row_id = pickle.load(fin)
        label = pickle.load(fin)
    return data, indices, indptr, row_id, label

def SaveFeatrues(file_path, data, indices, indptr, row_id, label):
    with open(file_path, 'wb') as fout:
        pickle.dump(data, fout)
        pickle.dump(indices, fout)
        pickle.dump(indptr, fout)
        pickle.dump(row_id, fout)
        pickle.dump(label, fout)

def ProcessFeatures(filepath, wifi_hashmap, mall_shop_hashmap, lng_lat, max_dist):
    with open(filepath, 'r') as fin:
        reader = csv.DictReader(fin)
        data = defaultdict(list)
        indices = defaultdict(list)
        indptr = defaultdict(list)
        row_id = defaultdict(list)
        label = defaultdict(list)
        row_count = 0
        for line in reader:
            row_count += 1
            mall_id = None
            if 'mall_id' in line:
                mall_id = line['mall_id']
                shop_index = -1
            else:
                assert 'shop_id' in line
                shop_id = line['shop_id']
                mall_id = mall_shop_hashmap.GetMallId(shop_id)
                shop_index = mall_shop_hashmap.GetShopIndex(mall_id, shop_id)

            # test data has row_id, but train date doesn't
            row_num = line['row_id'] if 'row_id' in line else row_count

            lng, lat = float(line['longitude']), float(line['latitude'])
            # filter (lng, lat) abnormal data
            if 'shop_id' in line:
                dist = math.sqrt((lng - lng_lat[shop_id][0])**2 + (lat - lng_lat[shop_id][1])**2)
                if dist > max_dist[shop_id]:
                    continue
            indptr[mall_id].append(len(indices[mall_id]))
            row_id[mall_id].append(row_num)
            label[mall_id].append(shop_index)
            col_num = 0
            if mall_id not in Config.bad_accuracy_mall_list:
                data[mall_id].append(lng), indices[mall_id].append(col_num)
                col_num += 1
                data[mall_id].append(lat), indices[mall_id].append(col_num)
                col_num += 1
            for wifi in line['wifi_infos'].split(';'):
                items = wifi.split('|')
                assert len(items) == 3
                bssid, signal, state = items[0], int(items[1]), bool(items[2])
                index = wifi_hashmap.GetIndex(mall_id, bssid)
                if index < 0:
                    continue;
                signal = -signal if state else signal
                data[mall_id].append(signal), indices[mall_id].append(col_num + index)
        for key in indptr.keys():
            indptr[key].append(len(indices[key]))
    return data, indices, indptr, row_id, label

def GetFeatures(filepath, wifi_hashmap, mall_shop_hashmap, lng_lat, max_dist):
    '''
    get features if having pickle file else processing raw data
    :param filepath:
    :param wifi_hashmap:
    :param mall_shop_hashmap:
    :return:
    '''
    pickle_file = filepath + '.pickle'
    if os.path.isfile(pickle_file):
        data, indices, indptr, row_id, label = LoadFeatures(pickle_file)
    else:
        data, indices, indptr, row_id, label = ProcessFeatures(filepath, wifi_hashmap, 
                mall_shop_hashmap, lng_lat, max_dist)
        SaveFeatrues(pickle_file, data, indices, indptr, row_id, label)

    dtrain_dict = {}
    for key in indptr.keys():
        csr = csr_matrix((data[key], indices[key], indptr[key]),
                         shape=(len(row_id[key]), wifi_hashmap.GetWifiInMall(key) + 2))
        dtrain_dict[key] = xgb.DMatrix(csr, label=label[key])
        LOGGER.info('mall_id={}||shape={}'.format(key, csr.shape))
    return dtrain_dict, row_id

def GetShopLngLat(shop_info_filepath):
    with open(shop_info_filepath, 'r') as fin:
        reader = csv.DictReader(fin)
        lng_lat = {}
        for line in reader:
            shop_id = line['shop_id']
            lng, lat = float(line['longitude']), float(line['latitude'])
            lng_lat[shop_id] = (lng, lat)
        return lng_lat

def GetShopMaxDist(shop_info_filepath, lng_lat):
    with open(shop_info_filepath, 'r') as fin:
        reader = csv.DictReader(fin)
        shop_user_dist_list = defaultdict(list)
        for line in reader:
            lng, lat = float(line['longitude']), float(line['latitude'])
            shop_id = line['shop_id']
            shop_user_dist_list[shop_id].append(
                math.sqrt((lng - lng_lat[shop_id][0])**2 +
                          (lat - lng_lat[shop_id][1])**2)
            )
        max_dist = defaultdict(float)
        for shop_id in shop_user_dist_list:
            shop_user_dist_list[shop_id].sort()
            l = len(shop_user_dist_list[shop_id])
            max_dist[shop_id] = shop_user_dist_list[shop_id][int(l * 0.9)] if l > 10 else \
                shop_user_dist_list[shop_id][-1]
        return max_dist
    

def Train(data_dir, wifi_hashmap, mall_shop_hashmap):
    shop_info_file = os.path.join(data_dir, Config.shop_info_filename)
    lng_lat = GetShopLngLat(shop_info_file)
    max_dist = GetShopMaxDist(shop_info_file, lng_lat)
    train_file = os.path.join(data_dir, Config.user_shop_filename)
    dtrain_dict, row_id = GetFeatures(train_file, wifi_hashmap, mall_shop_hashmap, lng_lat, max_dist)
    test_file = os.path.join(data_dir, Config.evaluation_filename)
    dtest_dict, row_id = GetFeatures(test_file, wifi_hashmap, mall_shop_hashmap, lng_lat, max_dist)
    assert dtrain_dict.keys() == dtest_dict.keys()

    # setup parameters for xgboost
    param = {}
    # use softmax multi-class classification
    param['objective'] = 'multi:softmax'
    # scale weight of positive examples
    param['eta'] = 0.1
    param['max_depth'] = 4
    param['silent'] = 1
    param['min_child_weight'] = 3
    # param['nthread'] = 2
    result = defaultdict(list)
    time_suffix = datetime.now().strftime('%Y_%m_%d_%H_%M')
    LOGGER.info(dtrain_dict.keys())
    for key in dtrain_dict.keys():
        if key not in Config.bad_accuracy_mall_list:
            continue
        param['num_class'] = mall_shop_hashmap.GetShopNumInMall(key)
        early_stop_round = 10
        if Config.is_train:
            error_list = xgb.cv(param, dtrain_dict[key],
                         num_boost_round=100,
                         nfold=4,
                         early_stopping_rounds=early_stop_round
                         )
            booster = xgb.train(param, dtrain_dict[key],
                                num_boost_round=len(error_list))
            model_path = os.path.join(data_dir, 'model_{}_{}'.format(key, time_suffix))
            booster.save_model(model_path)
            LOGGER.info(key)
            LOGGER.info(error_list)
        else:
            model_path = os.path.join(data_dir, 'model_{}_{}'.format(key, Config.selected_model_suffix))
            assert os.path.isfile(model_path)
            booster = xgb.Booster(model_file=model_path);

        prediction = booster.predict(dtest_dict[key])
        result[key] = []
        for p in prediction:
            result[key].append(mall_shop_hashmap.GetShopId(key, int(p)))
    result_filepath = os.path.join(
        data_dir,
        'predict_{}.csv'.format(time_suffix))

    with open(result_filepath, 'w') as fout:
        fout.write('row_id,shop_id\n')
        for key in dtest_dict.keys():
            for i in range(len(result[key])):
                fout.write('{},{}\n'.format(row_id[key][i], result[key][i]))


if __name__ == '__main__':
    LOGGER.info('start_time={}'.format(datetime.now()))
    data_dir = Config.data_dir
    wifi_hashmap = MallWifiMap(data_dir)
    mall_shop_hashmap = MallShopMap(data_dir)
    data = defaultdict(list)
    Train(data_dir, wifi_hashmap, mall_shop_hashmap)
    LOGGER.info('end_time={}'.format(datetime.now()))


