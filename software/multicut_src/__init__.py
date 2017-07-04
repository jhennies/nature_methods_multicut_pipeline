# TODO clean this up
from DataSet import DataSet, load_dataset

from MCSolver import multicut_workflow, lifted_multicut_workflow, multicut_workflow_with_defect_correction, lifted_multicut_workflow_with_defect_correction
from ExperimentSettings import ExperimentSettings
from Postprocessing import merge_small_segments, remove_small_segments, postprocess_with_watershed
from lifted_mc import compute_and_save_long_range_nh, optimize_lifted, compute_and_save_lifted_nh

from MCSolverImpl import probs_to_energies
from EdgeRF import learn_and_predict_rf_from_gt, RandomForest

from false_merges import compute_false_merges, resolve_merges_with_lifted_edges, project_resolved_objects_to_segmentation
from false_merges import resolve_merges_with_lifted_edges_global
from false_merges import shortest_paths, path_feature_aggregator
#from resolve_false_merges import compute_false_merges, resolve_merges_with_lifted_edges
#from resolve_false_merges import PipelineParameters
#from detect_false_merges import RemoveSmallObjectsParams, pipeline_remove_small_objects

from defect_handling import postprocess_segmentation
from defect_handling import get_delete_edges, get_skip_edges, get_skip_starts, get_skip_ranges, modified_edge_features

from tools import edges_to_volume, find_matching_row_indices

from workflow_no_learning import multicut_workflow_no_learning, costs_from_affinities, mala_clustering_workflow, lifted_multicut_workflow_no_learning


import logging
import sys


# Create the Logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Create the Handler for logging data to a file
logger_handler = logging.FileHandler('python_logging.log')
logger_handler.setLevel(logging.DEBUG)

# Create a Formatter for formatting the log messages
# logger_formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
logger_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')

# Add the Formatter to the Handler
logger_handler.setFormatter(logger_formatter)

# Create handler for output to the console
h_stream = logging.StreamHandler(sys.stdout)
h_stream.setLevel(logging.DEBUG)
h_stream.setFormatter(logger_formatter)

# Add the Handlers to the Logger
logger.addHandler(logger_handler)
logger.addHandler(h_stream)

logger.info('Configuration of multicut workflow logger complete!')