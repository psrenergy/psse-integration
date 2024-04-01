from __future__ import print_function
import argparse
import csv
import os
import sys
import psr.graf


_HAS_PANDAS = False
try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    pd = None

_HAS_GRAF = False
try:
    import psr.graf
    _HAS_GRAF = True
except ImportError:
    psr = None
    pass

_DEBUG_PRINT = False

_MOCK_PSSPY = False

_NCP_SUFFIX = "cp"


_PLANT_TYPE_OUTPUT_MAP = {
    "hydroplant": "gerhid",
    "thermalplant": "gerter",
    "renewableplant": "gergnd",
    "battery": "gerbat",
    "csp": "cspgen",
    "powerinjection": "powinj",
}

_BUS_LOAD_FILE = "demxba"

_DURATION_FILE = "duraci"


if "psspy" not in sys.modules:
    psspy = None
    _i = None
    _f = None
    _s = None


class MockPsspy:
    def case(self, *args):
        return 0

    def machine_chng_2(self, *args):
        return 0

    def load_chng_4(self, *args):
        return 0

    def fdns(self, *args):
        return 0

    def save(self, *args):
        return 0


def remove_plick(text):
    # type: (str) -> str
    return text.replace("'", "").replace("\"", "")


def _initialize_psse(psse_path):
    # type: (str) -> None
    print("Initializing PSS/E")
    global psspy
    global redirect
    global _i, _f, _s

    if not _MOCK_PSSPY:
        if psse_path not in sys.path:
            sys.path.append(psse_path)
            os.environ['PATH'] = os.environ['PATH'] + ';' + psse_path

        import psspy
        psspy.psseinit(100000)
        _i = psspy.getdefaultint()
        _f = psspy.getdefaultreal()
        _s = psspy.getdefaultchar()

        if "psspy" not in sys.modules:
            import redirect
            redirect.py2psse()
    else:
        psspy = MockPsspy()
        _i = None
        _f = None
        _s = None


class PlantMapEntry:
    def __init__(self):
        self.plant = SddpPlant()
        self.weight = 0.0
        self.machine_bus = 0
        self.machine_id = ""


class LoadMapEntry:
    def __init__(self):
        self.sddp_bus_name = ""
        self.weight = 0.0
        self.load_bus = 0
        self.load_id = ""


class SddpPlant:
    def __init__(self):
        self.system = ""
        self.name = ""
        self.type = ""
        self.bus = 0

    def __hash__(self):
        return hash("{},{},{},{}".format(self.system, self.name,
                                         self.type, self.bus))

    def __eq__(self, other):
        return hash(self) == hash(other)


class SddpScenario:
    def __init__(self):
        self.stage = 0
        self.scenario = 0
        self.block = 0

    def __hash__(self):
        return hash("{},{},{}".format(self.stage, self.scenario, self.block))

    def __eq__(self, other):
        return hash(self) == hash(other)

    def as_tuple(self):
        return self.stage, self.scenario, self.block


def _read_plant_map(plant_map_file_path):
    # type: (str) -> dict
    entries = {}
    with open(plant_map_file_path, "r") as csv_file:
        reader = csv.reader(csv_file)
        next(reader)
        for row in reader:
            sddp_plant = SddpPlant()
            sddp_plant.system = row[0].strip().lower()
            sddp_plant.type = row[1].strip().lower()
            sddp_plant.name = row[2].strip()
            sddp_plant.bus = int(row[3].strip())
            entry = PlantMapEntry()
            entry.plant = sddp_plant
            entry.weight = float(row[4])
            entry.machine_bus = int(row[5].strip())
            entry.machine_id = remove_plick(row[6].strip())

            if sddp_plant not in entries.keys():
                entries[sddp_plant] = [entry, ]
            else:
                entries[sddp_plant].append(entry)
    return entries


def _read_load_map(load_map_file_path):
    # type: (str) -> dict
    entries = {}
    with open(load_map_file_path, "r") as csv_file:
        reader = csv.reader(csv_file)
        next(reader)
        for row in reader:
            sddp_load_name = row[0].strip().lower()
            entry = LoadMapEntry()
            entry.sddp_bus_name = row[0].strip().lower()
            entry.weight = float(row[1])
            entry.load_bus = int(row[2].strip())
            entry.load_id = remove_plick(row[3].strip())

            if sddp_load_name not in entries.keys():
                entries[sddp_load_name] = [entry, ]
            else:
                entries[sddp_load_name].append(entry)
    return entries


def _read_scenario_map(scenario_map_file_path):
    # type: (str) -> None
    entries = {}
    with open(scenario_map_file_path, "r") as csv_file:
        reader = csv.reader(csv_file)
        next(reader)
        for row in reader:
            sddp_scenario = SddpScenario()
            sddp_scenario.stage = int(row[0])
            sddp_scenario.scenario = int(row[1])
            sddp_scenario.block = int(row[2])
            file_name = row[3].strip()
            entries[sddp_scenario] = file_name
    return entries


def _redistribute_weights(plant_map):
    # type: (dict) -> None
    for plant in plant_map.keys():
        entries = plant_map[plant]
        total_weight = sum([entry.weight for entry in entries])
        for entry in entries:
            entry.weight /= total_weight


def _get_required_plant_types(plant_map):
    # type: (dict) -> set
    types = set()
    for plant in plant_map.keys():
        types.add(plant.type)
    return types


def _load_graf_data(base_file_path, encoding, extensions):
    # type: (str, str, tuple) -> Union[psr.graf.CsvReader, psr.graf.BinReader, pd.DataFrame, None]
    extensions_to_try = extensions
    for ext in extensions_to_try:
        file_path = base_file_path + ext
        if os.path.exists(file_path):
            if _HAS_PANDAS:
                df = psr.graf.load_as_dataframe(
                    file_path, encoding=encoding)
                # rename columns to lower case
                df.columns = [col.lower() for col in df.columns]
                return df
            else:
                ReaderClass = psr.graf.CsvReader if ext == ".csv" else \
                    psr.graf.BinReader
                obj = ReaderClass()
                obj.open(file_path, encoding=encoding)

                return obj
    return None


def _load_plant_types_generation(sddp_case_path, plant_types, encoding,
                                 model, extensions):
    # type: (str, set, str, str, tuple) -> dict
    suffix = _NCP_SUFFIX if model == "ncp" else ""
    generation_df = {}
    for plant_type in plant_types:
        base_file_name = os.path.join(sddp_case_path,
                                      _PLANT_TYPE_OUTPUT_MAP[plant_type] + suffix)
        generation_df[plant_type] = _load_graf_data(base_file_name,
                                                    encoding=encoding,
                                                    extensions=extensions)
    return generation_df


def _get_required_psse_generators_names(plant_map, load_map):
    # type: (dict, dict) -> (set, set)
    generators = set()
    for plant in plant_map.keys():
        entries = plant_map[plant]
        for entry in entries:
            generators.add((entry.machine_bus, entry.machine_id))
    loads = set()
    for bus_name, entries in load_map.items():
        for entry in entries:
            loads.add((entry.load_bus, entry.load_id))
    return generators, loads


def _load_load_load(sddp_case_path, encoding, model, extensions):
    # type: (str, str, str, tuple) -> dict
    suffix = _NCP_SUFFIX if model == "ncp" else ""
    base_file_name = os.path.join(sddp_case_path, _BUS_LOAD_FILE + suffix)
    return _load_graf_data(base_file_name, extensions=extensions,
                           encoding=encoding)


def _read_binf_from_sddp(sddp_case_path):
    # type: (str) -> bool
    binf_file_path = os.path.join(sddp_case_path, "sddp.dat")
    if os.path.exists(binf_file_path):
            with open(binf_file_path, "r") as binf_file:
                for _ in range(52):
                    next(binf_file)
                for line in binf_file:
                    options = line.split(" ")
                    keyword = options[0]
                    if "BINF" == keyword.upper():
                        try:
                            value = int(options[1])
                            return value == 1
                        except ValueError:
                            return False
    return False


def main():
    # parse arguments
    # accepts an argument with the PF project name
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--psse_path", help="PSS/e installation path")
    parser.add_argument("-c", "--case_path", help="PSS/e sav file")
    parser.add_argument("-e", "--encoding", help="Result files encoding",
                        default="utf-8")
    # model should be sddp or ncp
    parser.add_argument("-m", "--model", help="Model to use",
                        default="sddp")

    # Sddp case path
    parser.add_argument("-sp", "--path", help="SDDP case path",
                        default=".")
    args = parser.parse_args()

    psse_path = args.psse_path
    psse_case_path = os.path.abspath(args.case_path)
    sddp_case_path = args.path
    encoding = args.encoding
    model = args.model.lower()

    if model not in ("sddp", "ncp"):
        print("Invalid model", model)
        sys.exit(1)

    update_dispatch(psse_path, psse_case_path, sddp_case_path,
                    encoding=encoding, model=model)


def update_dispatch(psse_path, psse_case_path, sddp_case_path, **kwargs):
    global psspy, _i, _f, _s
    encoding = kwargs.get("encoding", "utf-8")
    model = kwargs.get("model", "sddp")
    scenario_names_path = "scenarios_names.csv"
    if _DEBUG_PRINT:
        print("Reading scenarios_names.csv")
    scenario_names = _read_scenario_map(scenario_names_path)

    if _DEBUG_PRINT:
        print("Reading durations")

    if model == "ncp":
        extensions = (".csv", )
    else:
        using_binary_files = _read_binf_from_sddp(sddp_case_path)
        if using_binary_files:
            extensions = (".hdr", )
        else:
            extensions = (".csv", )

    durations_df = _load_graf_data(os.path.join(sddp_case_path,
                                                _DURATION_FILE),
                                   encoding=encoding, extensions=extensions)

    if _DEBUG_PRINT:
        print("Reading plant -> generator map")
    plant_map_path = "sddp_plant_psse_generator_map.csv"
    plant_map = _read_plant_map(plant_map_path)
    plant_types = _get_required_plant_types(plant_map)
    generation_df = _load_plant_types_generation(sddp_case_path, plant_types,
                                                 encoding, model,
                                                 extensions=extensions)

    if _DEBUG_PRINT:
        print("Reading Sddp Bus Load -> PSSE load map")
    load_map_path = "sddp_psse_load_map.csv"
    load_map = _read_load_map(load_map_path)
    load_df = _load_load_load(sddp_case_path, encoding=encoding,
                              extensions=extensions, model=model)

    psse_machines, psse_loads = _get_required_psse_generators_names(plant_map,
                                                                    load_map)

    if _DEBUG_PRINT:
        print("Starting psspy")

    if "psspy" not in sys.modules:
        _initialize_psse(psse_path)

    iret = psspy.case(psse_case_path)
    if iret != 0:
        # to stderr
        print("Error loading case", psse_case_path, file=sys.stderr)
        sys.exit(1)

    if _DEBUG_PRINT:
        print("Loaded generators")
        print(psse_machines)
        print("Loaded Scenarios")
        print(scenario_names)
    for scenario, scenario_name in scenario_names.items():
        if _HAS_PANDAS:
            scn_tuple = scenario.stage, scenario.block
            scenario_duration_h = durations_df.loc[scenario.as_tuple(),
                                                   :][0][0]
        else:
            agents = durations_df.agents
            fixed_scenario = 1
            all_values = durations_df.read(scenario.stage, fixed_scenario,
                                           scenario.block)
            scenario_duration_h = all_values[0]
        units_conversion = 1000.0 / scenario_duration_h

        # Update machines
        for machine_bus, machine_id in psse_machines:
            if _DEBUG_PRINT:
                print("Setting machine", machine_bus, machine_id)
            # search for related plant in plant map
            # TODO: maybe an inverse map is better
            plant = None
            weight = 1.0
            for sddp_plant, plant_map_entries in plant_map.items():
                for entry in plant_map_entries:
                    if machine_bus == entry.machine_bus and \
                            machine_id == entry.machine_id:
                        plant = sddp_plant
                        weight = entry.weight
                        break
                if plant is not None:
                    break
            if plant is None:
                print("Plant not found for generator:", machine_bus,
                      machine_id)
            else:
                if _DEBUG_PRINT:
                    print("Plant found:", plant.system, plant.type, plant.name)
                plant_type = plant.type
                plant_name = plant.name
                gen_type_df = generation_df[plant_type]
                if _HAS_PANDAS:
                    sddp_value = gen_type_df.loc[scenario.as_tuple(),
                                                 plant_name.lower()][0][0]
                else:
                    agents = gen_type_df.agents
                    all_values = gen_type_df.read(scenario.stage,
                                                  scenario.scenario,
                                                  scenario.block)
                    sddp_value = all_values[agents.index(plant_name)]
                value = sddp_value * units_conversion * weight
                if _DEBUG_PRINT:
                    print("Value read:", sddp_value, "Value assigned:", value)
                ierr = psspy.machine_chng_2(machine_bus, machine_id,
                                            [_i, _i, _i, _i, _i, _i],
                                            [value,_f,_f,_f,_f,_f,_f,_f,_f,
                                             _f,_f,_f,_f,_f,_f,_f,_f])

        # Update loads
        for load_bus, load_id in psse_loads:
            if _DEBUG_PRINT:
                print("Setting load", load_bus, load_id)
            bus_name = None
            weight = 1.0
            for sddp_bus_name, load_map_entries in load_map.items():
                for entry in load_map_entries:
                    if load_bus == entry.load_bus and \
                            load_id == entry.load_id:
                        bus_name = sddp_bus_name
                        weight = entry.weight
                        break
                if bus_name is not None:
                    break
            if bus_name is None:
                print("Sddp Load not found for PSSE Load:", load_bus, load_id)
            else:
                if _DEBUG_PRINT:
                    print("Bus Load found:", bus_name)
                if _HAS_PANDAS:
                    sddp_value = load_df.loc[scenario.as_tuple(),
                                             bus_name][0][0]
                else:
                    agents = [agent.lower() for agent in load_df.agents]
                    all_values = load_df.read(scenario.stage,
                                              scenario.scenario,
                                              scenario.block)
                    sddp_value = all_values[agents.index(bus_name)]
                value = sddp_value * units_conversion * weight
                if _DEBUG_PRINT:
                    print("Value read:", sddp_value, "Value assigned:", value)

                # load:
                ierr = psspy.load_chng_4(load_bus, load_id,
                                         [_i,_i,_i,_i,_i,_i],
                                         [value,_f,_f,_f,_f,_f])

        print("Solving case")
        psspy.fdns([0, 0, 0, 1, 1, 0, 99, 0])
        print("Saving to", scenario_name)
        psspy.save(scenario_name)

        if _DEBUG_PRINT:
            print("Scenario created", scenario_name)
        # app.PrintInfo("Scenario created: " + scenario_name)
    print("Finished")
    # app.PrintInfo("Finished")


if __name__ == "__main__":
    main()
