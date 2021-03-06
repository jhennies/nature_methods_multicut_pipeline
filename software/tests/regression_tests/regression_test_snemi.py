import os
import vigra
import argparse
from regression_test_utils import init, run_mc, run_lmc, regression_test

from multicut_src import ExperimentSettings
from multicut_src import MetaSet
#from multicut_src import load_dataset

def regression_test_snemi(cache_folder, data_folder):

    # if the cache does not exist, create it
    if not os.path.exists( os.path.join(cache_folder, 'snmei_train') ):
        meta = init(cache_folder, data_folder, 'snemi')
    else:
        meta = MetaSet(cache_folder)
        meta.load()

    # isbi params
    params = ExperimentSettings()
    params.rf_cache_folder = os.path.join(cache_folder, "rf_cache")
    params.use_2d = True
    params.learn_fuzzy = True
    params.anisotropy_factor = 5.
    params.ignore_mask = False
    params.n_trees = 500
    params.weighting_scheme = "all"
    params.solver = "multicut_exact"
    params.lifted_neighborhood = 3

    local_feats_list  = ("raw", "prob", "reg", "topo")
    lifted_feats_list = ("cluster", "reg")

    ds_train = meta.get_dataset('snemi_train')
    ds_test  = meta.get_dataset('snemi_test')
    mc_seg  = run_mc( ds_train, ds_test, local_feats_list, params)
    gamma = 10000.
    lmc_seg = run_lmc(ds_train, ds_test, local_feats_list, lifted_feats_list, params, gamma)

    print "Regression Test MC..."
    # Eval differences with same parameters and according regression thresholds
    # vi-split: 0.0501385345177 -> 0.1
    vi_split_ref = 0.1
    # vi-merge: 0.049803253098 -> 0.1
    vi_merge_ref = 0.1
    # adaptred-ri: 0.0170138077554 -> 0.05
    adapted_ri_ref = 0.05
    regression_test(
            vigra.readHDF5(os.path.join(data_folder,'mc_seg.h5'), 'data'),
            mc_seg,
            vi_split_ref,
            vi_merge_ref,
            adapted_ri_ref
            )
    print "... passed"

    print "Regression Test LMC..."
    # FIXME why are these differences so big?
    # vi-split: 0.291149212478 0.141228313621 0.0536859650649
    regression_test(
            vigra.readHDF5(os.path.join(data_folder,'lmc_seg.h5'), 'data'),
            lmc_seg
            )
    print "... passed"


if __name__ == '__main__':
    regression_test_snemi(
            '/home/constantin/Work/home_hdd/cache/regression_tests',
            '/home/constantin/Work/neurodata_hdd/regression_test_data/snemi')
