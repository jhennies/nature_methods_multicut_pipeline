# script for multicut on anisotropic data, isbi style

import sys
# TODO FIXME maybe we need something similar for nifty_with_cplex
#try to import opengm, it will fail if cplex is not installed
#try:
#    from opengm.inference import IntersectionBased
#except ImportError:
#    print "##########################################################################"
#    print "#########            CPLEX LIBRARY HAS NOT BEEN FOUND!!!           #######"
#    print "##########################################################################"
#    print "######### you have cplex? run install-cplex-shared-libs.sh script! #######"
#    print "##########################################################################"
#    print "######### don't have cplex? apply for an academic license at IBM!  #######"
#    print "#########               see README.txt for details                 #######"
#    print "##########################################################################"
#    sys.exit(1)

import argparse
import os
import vigra
import numpy as np

# watershed on distance transform
from wsdt import wsDtSegmentation

from multicut_src import MetaSet
from multicut_src import DataSet
from multicut_src import multicut_workflow, lifted_multicut_workflow
from multicut_src import ExperimentSettings
from multicut_src import edges_to_volume


def process_command_line():

    parser = argparse.ArgumentParser(
            description='Run the multicut pipeline for anisotropic data from mebrane label groundtruth (ISBI2012 alike)')

    parser.add_argument('data_folder', type=str,
        help = 'Folder with the data to process, expected files:' + '\n'
        + 'raw_train.tif: raw data tif volume for training data' + '\n'
        + 'probabilities_train.tif: membrane probability tif volume for training data (needs membrane channel!)'
        + '\n' + 'groundtruth.tif: membrane labels tif volume for training data'
        + 'raw_test.tif: raw data tif volume for test data' + '\n'
        + 'probabilities_test.tif: membrane probability tif volume for test data (needs membrane channel!)' + '\n' + 'All tifs need to be in order xyz!')

    parser.add_argument('output_folder', type=str,
        help = 'Folder for cache and outputs')

    parser.add_argument('--use_lifted', type=bool, default=False,
        help = 'Enable lifted Multicut. This may improve the results, but takes substantially longer. Disabled per default.')

    args = parser.parse_args()

    return args


# tif slices to volume
def slices_to_vol(path):
    files = os.listdir(path)
    vol = vigra.readVolume( os.path.join(path, files[0]) )
    vol = vol.squeeze()
    vol = vol.view(np.ndarray)
    return vol


# tif volume to volume
def vol_to_vol(path):
    vol = vigra.readVolume( path + ".tif" )
    vol = vol.squeeze()
    vol = vol.view(np.ndarray)
    #print vol.shape
    #print type(vol)
    return vol


# we need to normalize the probabilities, if
# they are not 0 to 1
def normalize_if(probs):
    if probs.max() > 1:
        probs /= probs.max()
    return probs


def wsdt(prob_map):

    threshold  = 0.3
    minMemSize = 1
    minSegSize = 1
    sigMinima  = 2.0
    sigWeights = 2.6
    groupSeeds = False

    segmentation = np.zeros_like(prob_map, dtype = np.uint32)
    offset = 0
    for z in xrange(prob_map.shape[2]):
        wsdt, _ = wsDtSegmentation(prob_map[:,:,z], threshold,
                minMemSize, minSegSize,
                sigMinima, sigWeights, groupSeeds)

        if not 0 in wsdt:
            wsdt -= 1

        segmentation[:,:,z] = wsdt
        segmentation[:,:,z] += offset
        offset = np.max(segmentation) + 1

    return segmentation


def labels_to_dense_gt( labels_path, probs):
    import vigra.graphs as vgraph

    labels = vol_to_vol(labels_path)
    gt     = np.zeros_like(labels, dtype = np.uint32)

    offset = 0
    for z in xrange(gt.shape[2]):

        hmap = vigra.filters.gaussianSmoothing( probs[:,:,z], 2.)
        seeds = vigra.analysis.labelImageWithBackground( labels[:,:,z] )
        gt[:,:,z], _ = vigra.analysis.watershedsNew(hmap, seeds = seeds)
        gt[:,:,z][ gt[:,:,z] != 0 ] += offset
        offset = gt[:,:,z].max()

    # bring to 0 based indexing
    gt -= 1

    # remove isolated segments
    rag_global = vgraph.regionAdjacencyGraph( vgraph.gridGraph(gt.shape[0:3]), gt)

    node_to_node = np.concatenate(
            [ np.arange(rag_global.nodeNum, dtype = np.uint32)[:,None] for _ in range(2)]
            , axis = 1 )

    for z in xrange(gt.shape[2]):
        rag_local = vgraph.regionAdjacencyGraph( vgraph.gridGraph(gt.shape[0:2]), gt[:,:,z])
        for node in rag_local.nodeIter():
            neighbour_nodes = []
            for nnode in rag_local.neighbourNodeIter(node):
                neighbour_nodes.append(nnode)
            if len(neighbour_nodes) == 1:
                node_coordinates = np.where(gt == node.id)
                if not 0 in node_coordinates[0] and not 511 in node_coordinates[0] and not 0 in node_coordinates[1] and not 511 in node_coordinates[1]:
                    node_to_node[node.id] = neighbour_nodes[0].id

    gt_cleaned = rag_global.projectLabelsToBaseGraph(node_to_node)[:,:,:,0]

    return gt_cleaned



def init(data_folder, cache_folder):
    meta = MetaSet(cache_folder)

    print "Generating initial cache, this may take some minutes"

    # init train and test data
    ds_train = DataSet(cache_folder, "ds_train")

    raw_train = vol_to_vol( os.path.join(data_folder, "raw_train") ).astype(np.float32)
    ds_train.add_raw_from_data(raw_train)

    probs_train = vol_to_vol( os.path.join(data_folder, "probabilities_train") )
    probs_train = normalize_if(probs_train)
    ds_train.add_input_from_data(probs_train)

    seg_train = wsdt( probs_train)
    ds_train.add_seg_from_data(seg_train)

    gt_train = labels_to_dense_gt( os.path.join(data_folder, "groundtruth"), probs_train)
    ds_train.add_gt_from_data(gt_train)

    # cutouts for training the lifted MC
    shape = ds_train.shape
    z0 = 0
    z1 = int( shape[2] * 0.2 )
    z2 = int( shape[2] * 0.8 )
    z3 = shape[2]

    ds_train.make_cutout([0, shape[0], 0, shape[1], z0, z1])
    ds_train.make_cutout([0, shape[0], 0, shape[1], z1, z2])
    ds_train.make_cutout([0, shape[0], 0, shape[1], z2, z3])

    meta.add_dataset("ds_train", ds_train)

    ds_test = DataSet(cache_folder, "ds_test")

    raw_test = vol_to_vol( os.path.join(data_folder, "raw_test") ).astype(np.float32)
    ds_test.add_raw_from_data(raw_test)

    probs_test = vol_to_vol( os.path.join(data_folder, "probabilities_test") ).astype(np.float32)
    probs_train = normalize_if(probs_test)
    ds_test.add_input_from_data(probs_test)

    seg_test = wsdt( probs_test )
    ds_test.add_seg_from_data(seg_test)

    meta.add_dataset("ds_test", ds_test)

    meta.save()


def main():
    args = process_command_line()

    out_folder = args.output_folder
    assert os.path.exists(out_folder), "Please choose an existing folder for the output"
    cache_folder = os.path.join(out_folder, "cache")

    # if the meta set wasn't saved yet, we need to recreate the cache
    if not os.path.exists( os.path.join(cache_folder, "meta_dict.pkl" ) ):
        init(args.data_folder, cache_folder)

    meta = MetaSet(cache_folder)
    meta.load()

    ds_train = meta.get_dataset("ds_train")
    ds_test  = meta.get_dataset("ds_test")

    # experiment settings
    exp_params = ExperimentSettings()

    exp_params.set_rfcache( os.path.join(cache_folder, "rf_cache") )

    # use extra 2d features
    exp_params.set_use2d(True)

    # parameters for learning
    exp_params.set_anisotropy(25.)
    exp_params.set_learn2d(True)
    exp_params.set_ignore_mask(False)
    exp_params.set_ntrees(500)

    exp_params.set_weighting_scheme("z")
    exp_params.set_solver("multicut_fusionmoves")

    local_feats_list  = ("raw", "prob", "reg", "topo")
    lifted_feats_list = ("mc", "cluster", "reg")

    seg_id = 0

    if args.use_lifted:
        print "Starting Lifted Multicut Workflow"

        # have to make filters first due to cutouts...
        ds_train.make_filters(0, exp_params.anisotropy_factor)
        ds_train.make_filters(1, exp_params.anisotropy_factor)
        ds_test.make_filters( 0, exp_params.anisotropy_factor)
        ds_test.make_filters( 1, exp_params.anisotropy_factor)

        mc_node, mc_edges, mc_energy, t_inf = lifted_multicut_workflow(
                ds_train, ds_test,
                seg_id, seg_id,
                local_feats_list, lifted_feats_list, exp_params,
                gamma = 2., warmstart = False, weight_z_lifted = True)

        save_path_seg = os.path.join(out_folder, "lifted_multicut_segmentation.tif")
        save_path_edge = os.path.join(out_folder, "lifted_multicut_labeling.tif")

    else:
        print "Starting Multicut Workflow"
        mc_node, mc_edges, mc_energy, t_inf = multicut_workflow(
                ds_train, ds_test,
                seg_id, seg_id,
                local_feats_list, exp_params)

        save_path_seg = os.path.join(out_folder, "multicut_segmentation.tif")
        save_path_edge = os.path.join(out_folder, "multicut_labeling.tif")

    mc_seg = ds_test.project_mc_result(seg_id, mc_node)

    print "Saving Segmentation Result to", save_path_seg
    vigra.impex.writeVolume(mc_seg, save_path_seg, '')

    # need to bring results back to the isbi challenge format...
    edge_vol = edges_to_volume(ds_test._rag(seg_id), mc_edges)
    print "Saving Edge Labeling Result to", save_path_edge
    vigra.impex.writeVolume(edge_vol, save_path_edge, '', dtype = np.uint8 )

if __name__ == '__main__':
    main()
