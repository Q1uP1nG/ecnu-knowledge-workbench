from .faculty import FacultyCollector
from .openalex import OpenAlexCollector
from .park import ScienceParkCollector
from .policies import PolicyCollector
from .transfer import TransferCollector

COLLECTORS = {
    "ecnu_faculty": FacultyCollector,
    "ecnu_openalex_papers": OpenAlexCollector,
    "ecnu_science_park": ScienceParkCollector,
    "ecnu_kjc_transfer": TransferCollector,
    "shanghai_policies": PolicyCollector,
}
