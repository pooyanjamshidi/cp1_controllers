import json


class ConfigurationDB:

    def __init__(self, conf_db):
        with open(conf_db) as config_file:
            data = json.load(config_file)
        self.db = data['configurations']

    def get_power_load(self, conf_id):
        pass

    def get_speed(self, conf_id):
        pass
