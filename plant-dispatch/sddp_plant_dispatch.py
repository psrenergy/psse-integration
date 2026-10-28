import argparse
import csv
import os
import sys

import psr.factory

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

# Result file index level -> SddpScenario attribute. NCP results are
# hourly, so "hour" plays the role of "block".
_INDEX_TO_SCENARIO_ATTR = {
    "stage": "stage",
    "scenario": "scenario",
    "block": "block",
    "hour": "block",
}


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

    def solved(self, *args):
        return 0

    def save(self, *args):
        return 0


def remove_plick(text: str) -> str:
    return text.replace("'", "").replace("\"", "")


def _initialize_psse(psse_path: str) -> None:
    global psspy
    global _i, _f, _s

    if _MOCK_PSSPY:
        psspy = MockPsspy()
        _i = None
        _f = None
        _s = None
        return

    if "psspy" in sys.modules:
        # launched from within PSS/E: already initialized
        import psspy
    else:
        print("Initializing PSS/E")
        if psse_path not in sys.path:
            sys.path.append(psse_path)
            os.environ['PATH'] = os.environ['PATH'] + ';' + psse_path

        import psspy
        psspy.psseinit(100000)
    _i = psspy.getdefaultint()
    _f = psspy.getdefaultreal()
    _s = psspy.getdefaultchar()


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


def _read_plant_map(plant_map_file_path: str) -> dict:
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


def _read_load_map(load_map_file_path: str) -> dict:
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


def _read_scenario_map(scenario_map_file_path: str) -> dict:
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


def _redistribute_weights(plant_map: dict) -> None:
    for plant in plant_map.keys():
        entries = plant_map[plant]
        total_weight = sum([entry.weight for entry in entries])
        for entry in entries:
            entry.weight /= total_weight


def _get_required_plant_types(plant_map: dict) -> set:
    types = set()
    for plant in plant_map.keys():
        types.add(plant.type)
    return types


class ResultData:
    """SDDP/NCP result file values, looked up by scenario and agent name."""

    def __init__(self, df: psr.factory.DataFrame, file_path: str):
        self.file_path = file_path
        self.index_names = list(df.index_names)
        for name in self.index_names:
            if name not in _INDEX_TO_SCENARIO_ATTR:
                raise ValueError("Unsupported index '{}' in {}".format(
                    name, file_path))
        # agent names are matched case-insensitively
        self.agents = {name.strip().lower(): i
                       for i, name in enumerate(df.column_names)}
        n_index = len(self.index_names)
        self._rows = {tuple(int(v) for v in row[:n_index]): row[n_index:]
                      for row in df.iter_rows()}

    def get(self, scenario: "SddpScenario",
            agent: str | None = None) -> float:
        """Value of agent (first agent if None) at the given scenario.

        Index levels absent from the file (e.g. scenario, for files that
        don't vary per scenario) are ignored.
        """
        key = tuple(getattr(scenario, _INDEX_TO_SCENARIO_ATTR[name])
                    for name in self.index_names)
        try:
            values = self._rows[key]
        except KeyError:
            raise KeyError("No {} {} in {}".format(
                self.index_names, key, self.file_path)) from None
        if agent is None:
            return float(values[0])
        try:
            column = self.agents[agent.strip().lower()]
        except KeyError:
            raise KeyError("Agent '{}' not found in {}".format(
                agent, self.file_path)) from None
        return float(values[column])


def _load_graf_data(base_file_path: str,
                    extensions: tuple[str, ...]) -> ResultData:
    for ext in extensions:
        file_path = base_file_path + ext
        if os.path.exists(file_path):
            df = psr.factory.load_dataframe(file_path, index_format="stage")
            return ResultData(df, file_path)
    raise FileNotFoundError("Result file not found: {}{}".format(
        base_file_path, "|".join(extensions)))


def _load_plant_types_generation(sddp_case_path: str, plant_types: set,
                                 model: str,
                                 extensions: tuple[str, ...]) -> dict:
    suffix = _NCP_SUFFIX if model == "ncp" else ""
    generation_df = {}
    for plant_type in plant_types:
        base_file_name = os.path.join(sddp_case_path,
                                      _PLANT_TYPE_OUTPUT_MAP[plant_type] + suffix)
        generation_df[plant_type] = _load_graf_data(base_file_name,
                                                    extensions=extensions)
    return generation_df


def _build_psse_element_maps(plant_map: dict,
                             load_map: dict) -> tuple[dict, dict]:
    """Inverse maps: (machine bus, id) -> (plant, weight) and
    (load bus, id) -> (sddp bus name, weight).

    Raises ValueError if a PSS/E element is mapped more than once.
    """
    errors = []
    machines = {}
    plant_names = {}
    for plant, entries in plant_map.items():
        name_key = (plant.type, plant.name.lower())
        other = plant_names.setdefault(name_key, plant)
        if other != plant:
            # result files identify plants by name only
            print("Warning: plants {}/{} and {}/{} share the name '{}' and "
                  "will read the same result column".format(
                      other.system, other.bus, plant.system, plant.bus,
                      plant.name), file=sys.stderr)
        for entry in entries:
            key = (entry.machine_bus, entry.machine_id)
            if key in machines:
                errors.append("Generator {} {} mapped to both '{}' and "
                              "'{}'".format(*key, machines[key][0].name,
                                            plant.name))
            else:
                machines[key] = (plant, entry.weight)
    loads = {}
    for bus_name, entries in load_map.items():
        for entry in entries:
            key = (entry.load_bus, entry.load_id)
            if key in loads:
                errors.append("Load {} {} mapped to both '{}' and "
                              "'{}'".format(*key, loads[key][0], bus_name))
            else:
                loads[key] = (bus_name, entry.weight)
    if errors:
        raise ValueError("Duplicated PSS/E elements in mapping files:\n"
                         + "\n".join(errors))
    return machines, loads


def _load_load_load(sddp_case_path: str, model: str,
                    extensions: tuple[str, ...]) -> ResultData:
    suffix = _NCP_SUFFIX if model == "ncp" else ""
    base_file_name = os.path.join(sddp_case_path, _BUS_LOAD_FILE + suffix)
    return _load_graf_data(base_file_name, extensions=extensions)


def _read_binf_from_sddp(sddp_case_path: str) -> bool:
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
    psr.factory.set_default_encoding(encoding)
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
                                   extensions=extensions)

    if _DEBUG_PRINT:
        print("Reading plant -> generator map")
    plant_map_path = "sddp_plant_psse_generator_map.csv"
    plant_map = _read_plant_map(plant_map_path)
    plant_types = _get_required_plant_types(plant_map)
    generation_df = _load_plant_types_generation(sddp_case_path, plant_types,
                                                 model, extensions=extensions)

    if _DEBUG_PRINT:
        print("Reading Sddp Bus Load -> PSSE load map")
    load_map_path = "sddp_psse_load_map.csv"
    load_map = _read_load_map(load_map_path)
    load_df = _load_load_load(sddp_case_path, extensions=extensions,
                              model=model)

    psse_machines, psse_loads = _build_psse_element_maps(plant_map, load_map)

    if _DEBUG_PRINT:
        print("Starting psspy")

    _initialize_psse(psse_path)

    if _DEBUG_PRINT:
        print("Loaded generators")
        print(list(psse_machines.keys()))
        print("Loaded Scenarios")
        print(scenario_names)
    not_converged = []
    failed = []
    for scenario, scenario_name in scenario_names.items():
        # start every scenario from the base case
        iret = psspy.case(psse_case_path)
        if iret != 0:
            raise RuntimeError("Error {} loading case {}".format(
                iret, psse_case_path))

        scenario_duration_h = durations_df.get(scenario)
        units_conversion = 1000.0 / scenario_duration_h
        n_errors = 0

        # Update machines
        for (machine_bus, machine_id), (plant, weight) in psse_machines.items():
            if _DEBUG_PRINT:
                print("Setting machine", machine_bus, machine_id,
                      "from plant", plant.system, plant.type, plant.name)
            gen_type_df = generation_df[plant.type]
            sddp_value = gen_type_df.get(scenario, plant.name)
            value = sddp_value * units_conversion * weight
            if _DEBUG_PRINT:
                print("Value read:", sddp_value, "Value assigned:", value)
            ierr = psspy.machine_chng_2(machine_bus, machine_id,
                                        [_i, _i, _i, _i, _i, _i],
                                        [value,_f,_f,_f,_f,_f,_f,_f,_f,
                                         _f,_f,_f,_f,_f,_f,_f,_f])
            if ierr != 0:
                n_errors += 1
                print("Error {} setting generator {} {}".format(
                    ierr, machine_bus, machine_id), file=sys.stderr)

        # Update loads
        for (load_bus, load_id), (bus_name, weight) in psse_loads.items():
            if _DEBUG_PRINT:
                print("Setting load", load_bus, load_id, "from", bus_name)
            sddp_value = load_df.get(scenario, bus_name)
            value = sddp_value * units_conversion * weight
            if _DEBUG_PRINT:
                print("Value read:", sddp_value, "Value assigned:", value)
            ierr = psspy.load_chng_4(load_bus, load_id,
                                     [_i,_i,_i,_i,_i,_i],
                                     [value,_f,_f,_f,_f,_f])
            if ierr != 0:
                n_errors += 1
                print("Error {} setting load {} {}".format(
                    ierr, load_bus, load_id), file=sys.stderr)

        if n_errors > 0:
            failed.append(scenario_name)

        print("Solving case")
        ierr = psspy.fdns([0, 0, 0, 1, 1, 0, 99, 0])
        solved = psspy.solved()
        if ierr != 0 or solved != 0:
            not_converged.append(scenario_name)
            print("Warning: {} did not converge (fdns error {}, solved "
                  "status {})".format(scenario_name, ierr, solved),
                  file=sys.stderr)

        print("Saving to", scenario_name)
        ierr = psspy.save(scenario_name)
        if ierr != 0:
            raise RuntimeError("Error {} saving {}".format(ierr,
                                                           scenario_name))

        if _DEBUG_PRINT:
            print("Scenario created", scenario_name)

    print("Finished: {} scenarios saved".format(len(scenario_names)))
    if failed:
        print("Scenarios with generator/load update errors:",
              ", ".join(failed), file=sys.stderr)
    if not_converged:
        print("Scenarios saved without a converged solution:",
              ", ".join(not_converged), file=sys.stderr)


if __name__ == "__main__":
    main()
