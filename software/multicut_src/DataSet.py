import numpy as np
import vigra
import vigra.graphs as graphs
import os
import h5py
from concurrent import futures
import itertools

import graph as agraph

from Tools import cacher_hdf5, cache_name


# TODO Flag that tells us, if we have flat or 3d superpixel
#      -> we can assert this every time we need flat superpix for a specific function
class DataSet(object):

    def __init__(self, meta_folder, ds_name):
        if not os.path.exists(meta_folder):
            os.mkdir(meta_folder)

        self.ds_name = ds_name
        self.cache_folder = os.path.join(meta_folder, self.ds_name)

        if not os.path.exists(self.cache_folder):
            os.mkdir(self.cache_folder)

        # Flag if raw data was added
        self.has_raw = False

        # Number of input data
        self.n_inp = 0

        # shape of input data
        self.shape = None

        # Number of segmentations
        self.n_seg = 0

        self.has_gt = False

        # cutouts, tesselations and inverse cutouts
        self.cutouts   = []
        self.n_cutouts = 0

        self.inverse_cutouts = {}

        # compression method used
        self.compression = 'gzip'

        # maximal anisotropy factor for filter calculation
        self.aniso_max = 20.

        # gt ids to be ignored for positive training examples
        self.gt_false_splits = set()
        # gt ids to be ignored for negative training examples
        self.gt_false_merges = set()

        # TODO add a defect mask instead -> most general
        self.defect_slices = []
        # if this is set to true, all defect calculations are ignored
        # dirty hack to make cutouts without defects work...
        #self.ignore_defects = False

        # segmentation mask -> learning + inference will be restriced to
        # superpixels in this mask
        self.has_seg_mask = False

        #
        # ignore values, because we don't want to hardcode this
        # TODO different values for different maskings ?!
        # TODO move to experiment params, once this is a singleton
        self.ignore_seg_value = 0 # for now this has to be zero, because this is the only value that vigra.relabelConsecutive conserves (but I can use my own impl of relabel)


    def __str__(self):
        return self.ds_name

    def add_defect_slices(self, defect_slice_list):
        assert self.has_raw
        assert isinstance(defect_slice_list, list)
        for z in defect_slice_list:
            assert z < self.shape[2]
        self.defect_slices = defect_slice_list

    def add_false_split_gt_id(self, gt_id):
        self.gt_false_splits.add(gt_id)

    def add_false_merge_gt_id(self, gt_id):
        self.gt_false_merges.add(gt_id)

    # This needs to be called before add seg!
    def add_seg_mask(self, mask_path, mask_key):
        assert self.has_raw
        mask = vigra.readHDF5(mask_path, mask_key)
        assert all( np.unique(mask) == np.array([0,1]) ), str(np.unique(mask))
        self.has_seg_mask = True
        if self.n_seg > 0:
            print "WARNING: Adding a segmentation mask does not change existing segmentations."
        if isinstance(self, Cutout):
            mask = mask[self.bb]
        assert mask.shape == self.shape, str(mask.shape) + " , " + str(self.shape)
        save_path = os.path.join(self.cache_folder,"seg_mask.h5")
        vigra.writeHDF5(mask, save_path, 'data', compression = self.compression)

    def get_seg_mask(self):
        assert self.has_seg_mask
        save_path = os.path.join(self.cache_folder,"seg_mask.h5")
        return vigra.readHDF5(save_path, 'data')

    #
    # Interface for adding inputs, segmentations and groundtruth
    #

    def _add_raw(self, raw):
        assert len(raw.shape) == 3, "Only 3d data supported"
        self.shape = raw.shape
        save_path = os.path.join(self.cache_folder,"inp0.h5")
        vigra.writeHDF5(raw, save_path, "data", chunks = True)
        self.has_raw = True
        self.n_inp = 1


    # add the raw_data
    # expects hdf5 input
    # probably better to do normalization in a way suited to the data!
    def add_raw(self, raw_path, raw_key):
        if self.has_raw:
            raise RuntimeError("Rawdata has already been added")
        raw = vigra.readHDF5(raw_path, raw_key).view(np.ndarray)
        self._add_raw(raw)


    # add the raw_data from np.array
    def add_raw_from_data(self, raw):
        if self.has_raw:
            raise RuntimeError("Rawdata has already been added")
        assert isinstance(raw, np.ndarray)
        self._add_raw(raw)


    def _add_input(self, pixmap):
        assert pixmap.shape[:3] == self.shape, "Pixmap shape " + str(pixmap.shape) + "does not match " + str(self.shape)
        save_path = os.path.join(self.cache_folder, "inp" + str(self.n_inp) + ".h5" )
        vigra.writeHDF5(pixmap, save_path, "data", chunks = True)
        self.n_inp += 1


    # add additional input map
    # expects hdf5 input
    def add_input(self, inp_path, inp_key):
        if not self.has_raw:
            raise RuntimeError("Add Rawdata before additional pixmaps")
        pixmap = vigra.readHDF5(inp_path,inp_key)
        self._add_input(pixmap)


    # add additional input map
    # expects hdf5 input
    def add_input_from_data(self, pixmap):
        if not self.has_raw:
            raise RuntimeError("Add Rawdata before additional pixmaps")
        assert isinstance(pixmap, np.ndarray)
        self._add_input(pixmap)


    # return input with inp_id (0 corresponds to the raw data)
    def inp(self, inp_id):
        if inp_id >= self.n_inp:
            raise RuntimeError("Trying to read inp_id " + str(inp_id) + " but there are only " + str(self.n_inp) + " input maps")
        inp_path = os.path.join(self.cache_folder,"inp" + str(inp_id) + ".h5")
        return vigra.readHDF5(inp_path, "data").astype('float32')


    def _add_seg(self, seg):
        if isinstance(self, Cutout):
            seg = seg[self.bb]
        assert seg.shape == self.shape, "Seg shape " + str(seg.shape) + "does not match " + str(self.shape)
        if self.has_seg_mask:
            print "Cutting segmentation mask from seg"
            mask = self.get_seg_mask()
            assert self.ignore_seg_value == 0, "Only zero ignore value supported for now" # TODO change once we allow more general values
            seg[ np.logical_not(mask) ] = self.ignore_seg_value
            # TODO to allow other ignore values than zero, we need to use a different relabeling value here
            seg, _, _ = vigra.analysis.relabelConsecutive( seg.astype('uint32'),
                    start_label = 1,
                    keep_zeros = True)
        else:
            seg, _, _ = vigra.analysis.relabelConsecutive(seg.astype('uint32'), start_label = 0)
        save_path = os.path.join(self.cache_folder, "seg" + str(self.n_seg) + ".h5")
        vigra.writeHDF5(seg, save_path, "data", compression = self.compression)
        self.n_seg += 1


    # add segmentation of the volume
    # expects hdf5 input
    def add_seg(self, seg_path, seg_key):
        if not self.has_raw:
            raise RuntimeError("Add Rawdata before adding a segmentation")
        seg = vigra.readHDF5(seg_path, seg_key).astype('uint32')
        self._add_seg(seg)


    # add segmentation of the volume
    # expects hdf5 input
    def add_seg_from_data(self, seg):
        if not self.has_raw:
            raise RuntimeError("Add Rawdata before adding a segmentation")
        assert isinstance(seg, np.ndarray)
        self._add_seg(seg)


    # return segmentation with seg_id
    def seg(self, seg_id):
        if seg_id >= self.n_seg:
            raise RuntimeError("Trying to read seg_id " + str(seg_id) + " but there are only " + str(self.n_seg) + " segmentations")
        seg_path = os.path.join(self.cache_folder,"seg" + str(seg_id) + ".h5")
        return vigra.readHDF5(seg_path, "data")


    def _add_gt(self, gt):
        if isinstance(self, Cutout):
            gt = gt[self.bb]
        assert gt.shape == self.shape, "GT shape " + str(gt.shape) + "does not match " + str(self.shape)
        # FIXME running a label volume might be helpful sometimes, but it can mess up the split and merge ids!
        # also messes up defects in cremi...
        #gt = vigra.analysis.labelVolumeWithBackground(gt.astype(np.uint32))
        #gt -= gt.min()
        save_path = os.path.join(self.cache_folder,"gt.h5")
        vigra.writeHDF5(gt, save_path, "data", compression = self.compression)
        self.has_gt = True


    # only single gt for now!
    # add grountruth
    def add_gt(self, gt_path, gt_key):
        if self.has_gt:
            raise RuntimeError("Groundtruth has already been added")
        gt = vigra.readHDF5(gt_path, gt_key)
        self._add_gt(gt)


    # only single gt for now!
    # add grountruth
    def add_gt_from_data(self, gt):
        if self.has_gt:
            raise RuntimeError("Groundtruth has already been added")
        assert isinstance(gt, np.ndarray)
        self._add_gt(gt)


    # get the groundtruth
    def gt(self):
        if not self.has_gt:
            raise RuntimeError("Need to add groundtruth first!")
        gt_path = os.path.join(self.cache_folder, "gt.h5")
        return vigra.readHDF5(gt_path, "data")


    # calculate the region adjacency graph of seg_id
    def _rag(self, seg_id):

        filename    = "rag_seg" + str(seg_id) + ".h5"
        ragpath = os.path.join(self.cache_folder, filename)
        rag_key  = "rag"
        if not os.path.isfile(ragpath):
            print "Computing RAG for seg_id", str(seg_id)
            grid = graphs.gridGraph(self.shape[0:3])
            _rag  = graphs.regionAdjacencyGraph(grid, self.seg(seg_id))

            print "WRITING IN ", ragpath, rag_key
            _rag.writeHDF5(ragpath, rag_key)

        else:
            #print "Loading RAG for seg_id", str(seg_id), "from HDF5:"
            #print ragpath
            #print rag_key
            _rag = vigra.graphs.loadGridRagHDF5(ragpath, rag_key)
        return _rag


    # get the segments adjacent to the edges for each edge
    @cacher_hdf5()
    def _adjacent_segments(self, seg_id):
        print "Getting segments adjacent to edges from RAG:"
        rag = self._rag(seg_id)

        adjacent_segs = rag.uvIds()
        adjacent_segs = np.sort(adjacent_segs, axis = 1)
        assert adjacent_segs.shape[0] == rag.edgeIds().shape[0]
        return adjacent_segs


    # get the adjacent edges for each edge
    # TODO can't cache this
    #@cacher_hdf5()
    def _adjacent_edges(self, seg_id):
        print "Getting adjacent edges from RAG:"
        rag = self._rag(seg_id)
        adjacent_edges = []
        for edge in rag.edgeIter():
            adj_to_edge = []
            n_1  = rag.u(edge)
            n_2  = rag.v(edge)
            for n in (n_1,n_2):
                for arc in rag.incEdgeIter(n):
                    new_edge = rag.edgeFromArc(arc)
                    if new_edge != edge:
                        adj_to_edge.append(new_edge.id)
            adjacent_edges.append(adj_to_edge)

        assert len(adjacent_edges) == rag.edgeNum, str(len(adjacent_edges)) + " , " + str(rag.edgeNum)
        return adjacent_edges


    # calculates the eccentricity centers for given seg_id
    @cacher_hdf5()
    def eccentricity_centers(self, seg_id, is_2d_stacked):
        seg = self.seg(seg_id)
        n_threads = 20 # TODO get this from global params
        if is_2d_stacked: # if we have a stacked segmentation, we can parallelize over the slices

            # calculate the centers for a 2d slice
            def centers_2d(z):
                seg_z = seg[:,:,z]
                min_label = seg_z.min()
                # eccentricity centers expect a consecutive labeling -> we only return the relevant part
                centers = vigra.filters.eccentricityCenters(seg_z)[min_label:]
                return [cent + (z,) for cent in centers] # extend by z coordinate

            with futures.ThreadPoolExecutor(max_workers = n_threads) as executor:
                tasks = [executor.submit(centers_2d, z) for z in xrange(seg.shape[2])]
                centers = [t.result() for t in tasks]
                # return flattened list
            centers_list = list(itertools.chain(*centers))
            n_segs = seg.max() + 1
            assert len(centers_list) == n_segs, "%i, %i" % (len(centers_list), n_segs)
            return centers_list
        else:
            return vigra.filters.eccentricityCenters(seg)


    #
    # Feature Calculation
    #

    # FIXME: Apperently the cacher does not accept keyword arguments
    # this will be ignorant of using a different segmentation
    @cacher_hdf5(ignoreNumpyArrays=True)
    def distance_transform(self, segmentation, anisotropy):

        # # if that does what I think it does (segmentation to edge image), we can use vigra...
        # def pixels_at_boundary(image, axes=[1, 1, 1]):
        #    return axes[0] * ((np.concatenate((image[(0,), :, :], image[:-1, :, :]))
        #                       - np.concatenate((image[1:, :, :], image[(-1,), :, :]))) != 0) \
        #           + axes[1] * ((np.concatenate((image[:, (0,), :], image[:, :-1, :]), 1)
        #                         - np.concatenate((image[:, 1:, :], image[:, (-1,), :]), 1)) != 0) \
        #           + axes[2] * ((np.concatenate((image[:, :, (0,)], image[:, :, :-1]), 2)
        #                         - np.concatenate((image[:, :, 1:], image[:, :, (-1,)]), 2)) != 0)
        #
        # anisotropy = np.array(anisotropy).astype(np.float32)
        # image = image.astype(np.float32)
        # # Compute boundaries
        # # FIXME why ?!
        # axes = (anisotropy ** -1).astype(np.uint8)
        # image = pixels_at_boundary(image, axes)

        edge_volume = np.concatenate(
                [vigra.analysis.regionImageToEdgeImage(segmentation[:,:,z])[:,:,None] for z in xrange(segmentation.shape[2])],
                axis = 2)
        dt = vigra.filters.distanceTransform(edge_volume, pixel_pitch=anisotropy, background=True)
        return dt


    # make pixelfilter for the given input.
    # the sigmas are scaled with the anisotropy factor
    # max. anisotropy factor is 20.
    # if it is higher, the features are calculated purely in 2d
    def make_filters(self,
            inp_id,
            anisotropy_factor,
            filter_names = [ "gaussianSmoothing",
                             "hessianOfGaussianEigenvalues",
                             "laplacianOfGaussian"],
            sigmas = [1.6, 4.2, 8.3]
            ):

        assert anisotropy_factor >= 1., "Finer resolution in z-direction is not supported"
        print "Calculating filters for input id:", inp_id
        # import fastfilters

        # FIXME dirty hack to calculate features on the ditance trafo
        # FIXME the dt must be pre-computed for this to work
        if inp_id == 'distance_transform':
            fake_seg = np.zeros((10,10))
            inp = self.distance_transform(fake_seg, [1.,1.,anisotropy_factor])
            input_name = 'distance_transform'
        else:
            assert inp_id < self.n_inp, str(inp_id) + " , " + str(self.n_inp)
            inp = self.inp(inp_id)
            input_name = "inp_" + str(inp_id)

        top_folder = os.path.join(self.cache_folder, "filters")
        if not os.path.exists(top_folder):
            os.mkdir(top_folder)

        # determine, how we calculate the pixfeats (2d, pure 3d or 3d scaled with anisotropy)
        # save filters to corresponding path
        calculation_2d = False

        if anisotropy_factor == 1.:
            filter_folder = os.path.join(top_folder, "filters_3d")
        elif anisotropy_factor >= self.aniso_max:
            filter_folder = os.path.join(top_folder, "filters_2d")
            calculation_2d = True
        else:
            filter_folder = os.path.join(top_folder, "filters_" + str(anisotropy_factor) )

        if not os.path.exists(filter_folder):
            os.makedirs(filter_folder)

        if not calculation_2d and anisotropy_factor > 1.:
            print "WARNING: Anisotropic feature calculation not supported in fastfilters yet."
            print "Using vigra filters instead."
            filter_names = [".".join( ("vigra.filters", filtname) ) for filtname in filter_names]
        else:
            filter_names = [".".join( ("vigra.filters", filtname) ) for filtname in filter_names]

        # update the filter folder to the input
        filter_folder = os.path.join( filter_folder, input_name )
        if not os.path.exists(filter_folder):
            os.mkdir(filter_folder)
        filter_key    = "data"

        # list of paths to the filters, that will be calculated
        return_paths = []
        # TODO set max_workers with ppl param value!
        n_workers = 8

        def _calc_filter_2d(filter_fu, sig, filt_path):
            filt_name = os.path.split(filt_path)[1].split(".")[-2].split('_')[0] # some string gymnastics to recover the name
            is_singlechannel = True if filt_name != "hessianOfGaussianEigenvalues" else False
            f_shape = inp.shape if is_singlechannel else inp.shape + (2,)
            chunks  = ( min(512,inp.shape[0]), min(512,inp.shape[2]), 1 ) if is_singlechannel else ( min(512,inp.shape[0]), min(512,inp.shape[2]), 1, 2)
            filter_res = np.zeros(f_shape, dtype = 'float32')
            for z in xrange(inp.shape[2]):
                filter_res[:,:,z] = filter_fu(inp[:,:,z], sig)
            vigra.writeHDF5(filter_res, filt_path, filter_key, chunks = chunks)

        def _calc_filter_3d(filter_fu, sig, filt_path):
            filter_res = filter_fu( inp, sig )
            vigra.writeHDF5(filter_res, filt_path, filter_key, chunks = True)

        if calculation_2d:
            print "Calculating Filter in 2d"
            _calc_filter = _calc_filter_2d
        else:
            print "Calculating filter in 3d, with anisotropy factor:", str(anisotropy_factor)
            if anisotropy_factor != 1.:
                sigmas = [(sig, sig, sig / anisotropy_factor) for sig in sigmas]
            _calc_filter = _calc_filter_3d

        with futures.ThreadPoolExecutor(max_workers = n_workers) as executor:
            tasks = []
            for filt_name in filter_names:
                filter_fu = eval(filt_name)
                for sig in sigmas:
                    # check whether this is already there
                    filt_path = os.path.join(filter_folder, filt_name + "_" + str(sig) )
                    if not os.path.exists(filt_path):
                        tasks.append( executor.submit(_calc_filter, filter_fu, sig, filt_path) )
                    return_paths.append(filt_path)
            res = [t.result() for t in tasks]

        return_paths.sort()
        return return_paths


    # accumulates the given filter over all edges in the
    # filter has to be given in the correct size!
    # Also Median, 0.25-Quantile, 0.75-Quantile, Kurtosis, Skewness
    # we can pass the rag, because loading it for large datasets takes some time...
    def _accumulate_filter_over_edge(self, seg_id, filt, filt_name, rag = None):
        assert len(filt.shape) in (3,4)
        assert filt.shape[0:3] == self.shape
        if rag == None:
            rag = self._rag(seg_id)
        # split multichannel features
        feats_return = []
        names_return = []
        if len(filt.shape) == 3:
            # let RAG do the work
            gridGraphEdgeIndicator = vigra.graphs.implicitMeanEdgeMap(rag.baseGraph, filt)
            #edgeFeat_mean = rag.accumulateEdgeFeatures(gridGraphEdgeIndicator)[:,np.newaxis]
            edgeFeats     = rag.accumulateEdgeStatistics(gridGraphEdgeIndicator)
            feats_return.append(edgeFeats)
            names_return.append("EdgeFeature_" + filt_name + "_mean")
            names_return.append("EdgeFeature_" + filt_name + "_sum")
            names_return.append("EdgeFeature_" + filt_name + "_min")
            names_return.append("EdgeFeature_" + filt_name + "_max")
            names_return.append("EdgeFeature_" + filt_name + "_variance")
            names_return.append("EdgeFeature_" + filt_name + "_skewness")
            names_return.append("EdgeFeature_" + filt_name + "_kurtosis")
            names_return.append("EdgeFeature_" + filt_name + "_0.1quantile")
            names_return.append("EdgeFeature_" + filt_name + "_0.25quantile")
            names_return.append("EdgeFeature_" + filt_name + "_0.5quantile")
            names_return.append("EdgeFeature_" + filt_name + "_0.75quantile")
            names_return.append("EdgeFeature_" + filt_name + "_0.90quantile")
        elif len(filt.shape) == 4:
            for c in range(filt.shape[3]):
                print "Multichannel feature, accumulating channel:", c + 1, "/", filt.shape[3]
                gridGraphEdgeIndicator = vigra.graphs.implicitMeanEdgeMap(
                        rag.baseGraph, filt[:,:,:,c] )
                #edgeFeat_mean = rag.accumulateEdgeFeatures(gridGraphEdgeIndicator)[:,np.newaxis]
                edgeFeats     = rag.accumulateEdgeStatistics(gridGraphEdgeIndicator)
                feats_return.append(edgeFeats)
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_mean")
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_sum")
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_min")
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_max")
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_variance")
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_skewness")
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_kurtosis")
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_0.1quantile")
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_0.25quantile")
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_0.5quantile")
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_0.75quantile")
                names_return.append("EdgeFeature_" + filt_name + "_c" + str(c)  + "_0.90quantile")
        return feats_return, names_return


    # Features from different filters, accumulated over the edges
    # hacked in for features from affinity maps
    @cacher_hdf5("feature_folder", True)
    def edge_features_from_affinity_maps(self, seg_id, inp_id, anisotropy_factor):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert inp_id < self.n_inp, str(inp_id) + " , " + str(self.n_inp)
        assert anisotropy_factor >= 20., "Affinity map features only for 2d filters."

        # import fastfilters

        filter_names = [ "gaussianSmoothing",
                         "hessianOfGaussianEigenvalues",
                         "laplacianOfGaussian"]
        sigmas = [1.6, 4.2, 8.3]

        #inp = self.inp(inp_id)
        #assert inp.ndim == 4, "Need affinity channels"
        #assert inp.shape[3] == 3, "Need 3 affinity channels"

        #inpXY = np.maximum( inp[:,:,:,0], inp[:,:,:,1] )
        #inpZ  = inp[:,:,:,2]

        rag = self._rag(seg_id)

        inpXY = self.inp(inp_id)
        inpZ = self.inp(inp_id+1)

        edge_indications = self.edge_indications(seg_id)
        edge_features = []

        n = 0
        N = len(filter_names) * len(sigmas)

        for fname in filter_names:
            filter_fu = eval(fname)
            for sigma in sigmas:
                print "Accumulating features:", n, "/", N

                # filters for xy channels
                with futures.ThreadPoolExecutor(max_workers = 35 ) as executor:
                    tasks = []
                    for z in xrange(inpXY.shape[2]):
                        tasks.append( executor.submit(filter_fu, inpXY[:,:,z], sigma ) )
                    filtXY = [task.result() for task in tasks]

                if filtXY[0].ndim == 2:
                    filtXY = np.concatenate([re[:,:,None] for re in filtXY], axis = 2)
                elif filtXY[0].ndim == 3:
                    filtXY = np.concatenate([re[:,:,None,:] for re in filtXY], axis = 2)

                # filters for xy channels
                with futures.ThreadPoolExecutor(max_workers = 20 ) as executor:
                    tasks = []
                    for z in xrange(inpZ.shape[2]):
                        tasks.append( executor.submit(filter_fu, inpZ[:,:,z], sigma ) )
                    filtZ = [task.result() for task in tasks]

                if filtZ[0].ndim == 2:
                    filtZ = np.concatenate([re[:,:,None] for re in filtZ], axis = 2)
                elif filtZ[0].ndim == 3:
                    filtZ = np.concatenate([re[:,:,None,:] for re in filtZ], axis = 2)

                # accumulate over the edge
                featsXY, _ = self._accumulate_filter_over_edge(seg_id, filtXY, "", rag)
                featsXY    = np.concatenate(featsXY, axis = 1)
                featsZ, _  = self._accumulate_filter_over_edge(seg_id, filtZ, "", rag)
                featsZ     = np.concatenate(featsZ,  axis = 1)

                feats = np.zeros_like(featsXY)
                feats[edge_indications==1] = featsXY[edge_indications==1]
                feats[edge_indications==0] = featsZ[edge_indications==0]

                edge_features.append(feats)
                n += 1

        edge_features = np.concatenate( edge_features, axis = 1)
        assert edge_features.shape[0] == len( rag.edgeIds() ), str(edge_features.shape[0]) + " , " +str(len( rag.edgeIds() ))

        # remove NaNs
        edge_features = np.nan_to_num(edge_features)

        return edge_features


    # Features from different filters, accumulated over the edges
    @cacher_hdf5("feature_folder", True)
    def edge_features(self, seg_id, inp_id, anisotropy_factor):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert inp_id < self.n_inp, str(inp_id) + " , " + str(self.n_inp)
        assert anisotropy_factor >= 1., "Finer resolution in z-direction is nor supported"

        # calculate the volume filters for the given input
        if isinstance(self, Cutout):
            filter_paths = self.make_filters(inp_id, anisotropy_factor, self.ancestor_folder)
        else:
            filter_paths = self.make_filters(inp_id, anisotropy_factor)

        filter_key = "data"

        rag = self._rag(seg_id)

        n = 0
        N = len(filter_paths)

        # iterate over all filters and accumulate the edge features
        edge_features = []
        edge_features_names = []
        for path in filter_paths:
            n += 1
            print "Accumulating features:", n, "/", N
            print "From:", path

            # load the precomputed filter from file
            with h5py.File(path) as f:
                f_shape = f[filter_key].shape

            # check whether the shapes match, otherwise get the correct shape
            if f_shape[0:3] != self.shape:
                assert isinstance(self, Cutout), "This should only happen in cutouts!"
                with h5py.File(path) as f:
                    filt = f[filter_key][self.bb]
            else:
                filt = vigra.readHDF5(path, filter_key)

            # FIXME deprecated
            ## now it gets hacky...
            ## for InverseCutouts, we have to remove the not covered part from the filter
            #if isinstance(self, InverseCutout):
            #    p = self.cut_coordinates
            #    filt[p[0]:p[1],p[2]:p[3],p[4]:p[5]] = 0

            # accumulate over the edge
            feats_acc, names_acc = self._accumulate_filter_over_edge(
                    seg_id,
                    filt,
                    os.path.split(path)[1],
                    rag)
            edge_features.extend(feats_acc)
            edge_features_names.extend(names_acc)

        edge_features = np.concatenate( edge_features, axis = 1)
        assert edge_features.shape[0] == len( rag.edgeIds() ), str(edge_features.shape[0]) + " , " +str(len( rag.edgeIds() ))

        # save the feature names to file
        save_folder = os.path.join(self.cache_folder, "features")
        if not os.path.exists(save_folder):
            os.mkdir(save_folder)
        # clip the anisotropy factor
        if anisotropy_factor >= self.aniso_max:
            anisotropy_factor = self.aniso_max
        save_name = "edge_features_" + str(seg_id) + "_" + str(inp_id) + "_" + str(anisotropy_factor) + ".h5"
        save_file = os.path.join( save_folder, save_name)
        vigra.writeHDF5(edge_features_names, save_file, "edge_features_names")

        # remove NaNs
        edge_features = np.nan_to_num(edge_features)

        return edge_features


    # get the name of the edge features
    def edge_features_names(self, seg_id, inp_id, anisotropy_factor):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert inp_id < self.n_inp, str(inp_id) + " , " + str(self.n_inp)
        self.edge_features(seg_id, inp_id, anisotropy_factor)

        save_folder = os.path.join(self.cache_folder, "features")
        if anisotropy_factor >= self.aniso_max:
            anisotropy_factor = self.aniso_max
        save_name = "edge_features_" + str(seg_id) + "_" + str(inp_id) + "_" + str(anisotropy_factor) + ".h5"
        save_file = os.path.join( save_folder, save_name)
        assert os.path.exists(save_file)

        return vigra.readHDF5(save_file,"edge_features_names")


    # get region statistics with the vigra region feature extractor
    @cacher_hdf5(folder = "feature_folder")
    def _region_statistics(self, seg_id, inp_id):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert inp_id < self.n_inp, str(inp_id) + " , " + str(self.n_inp)

        # list of the region statistics, that we want to extract
        statistics =  [ "Count", "Kurtosis", #"Histogram",
                        "Maximum", "Minimum", "Quantiles",
                        "RegionRadii", "Skewness", "Sum",
                        "Variance", "Weighted<RegionCenter>", "RegionCenter"]

        extractor = vigra.analysis.extractRegionFeatures(
                self.inp(inp_id).astype(np.float32),
                self.seg(seg_id).astype(np.uint32),
                features = statistics )

        node_features = np.concatenate(
            [extractor[stat_name][:,None].astype('float32') if extractor[stat_name].ndim == 1 else extractor[stat_name].astype('float32') for stat_name in statistics],
            axis = 1)

        reg_stat_names = list(itertools.chain.from_iterable(
            [ [stat_name for _ in xrange(extractor[stat_name].shape[1])] if extractor[stat_name].ndim>1 else [stat_name] for stat_name in statistics[:9] ] ))

        reg_center_names = list(itertools.chain.from_iterable(
            [ [stat_name for _ in xrange(extractor[stat_name].shape[1])] for stat_name in statistics[9:] ] ))

        # this is the number of node feats that are combined with min, max, sum, absdiff
        # in conrast to center feats, which are combined with euclidean distance
        n_stat_feats = 17 # magic_nu...

        save_path = cache_name("_region_statistics", "feature_folder", False, False, self, seg_id, inp_id)

        vigra.writeHDF5(node_features[:,:n_stat_feats], save_path, "region_statistics")
        vigra.writeHDF5(reg_stat_names, save_path, "region_statistics_names")
        vigra.writeHDF5(node_features[:,n_stat_feats:], save_path, "region_centers")
        vigra.writeHDF5(reg_center_names, save_path, "region_center_names")

        return statistics


    @cacher_hdf5(folder = "feature_folder", ignoreNumpyArrays=True)
    def region_features(self, seg_id, inp_id, uv_ids, lifted_nh):

        import gc

        if lifted_nh:
            print "Computing Lifted Region Features for NH:", lifted_nh
        else:
            print "Computing Region features for local Edges"

        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert inp_id < self.n_inp, str(inp_id) + " , " + str(self.n_inp)

        # make sure the region statistics are calcuulated
        self._region_statistics(seg_id, inp_id)
        region_statistics_path = cache_name("_region_statistics", "feature_folder", False, False, self, seg_id, inp_id)

        # if we have a segmentation mask, we don't calculate features for uv-ids which
        # include 0 (== everything outside of the mask)
        # otherwise the ram consumption for the lmc can blow up...
        if self.has_seg_mask:
            where_uv = (uv_ids != self.ignore_seg_value).all(axis = 1)
            # for lifted edges assert that no ignore segments are in lifted uvs
            if lifted_nh:
                assert np.sum(where_uv) == where_uv.size
            else:
                uv_ids = uv_ids[where_uv]

        # compute feature from region statistics
        regStats = vigra.readHDF5(region_statistics_path, 'region_statistics')
        regStatNames = vigra.readHDF5(region_statistics_path, 'region_statistics_names')

        fU = regStats[uv_ids[:,0],:]
        fV = regStats[uv_ids[:,1],:]

        allFeat = [
                np.minimum(fU, fV),
                np.maximum(fU, fV),
                np.abs(fU - fV),
                fU + fV
            ]

        feat_names = []
        feat_names.extend(
                ["RegionFeatures_" + name + combine for combine in  ("_min", "_max", "_absdiff", "_sum") for name in regStatNames  ])

        # we actively delete stuff we don't need to free memory
        # because this may become memory consuming for lifted edges
        fV = fV.resize((1,1))
        fU = fU.resize((1,1))
        regStats = regStats.resize((1,))
        del fU
        del fV
        del regStats
        gc.collect()

        # compute features from region centers
        regCenters = vigra.readHDF5(region_statistics_path, 'region_centers')
        regCenterNames = vigra.readHDF5(region_statistics_path, 'region_center_names')

        sU = regCenters[uv_ids[:,0],:]
        sV = regCenters[uv_ids[:,1],:]
        allFeat.append( (sU - sV)**2 )

        feat_names.extend(["RegionFeatures_" + name for name in regCenterNames])

        sV = sV.resize((1,1))
        sU = sU.resize((1,1))
        regCenters = regCenters.resize((1,1))
        del sU
        del sV
        del regCenters
        gc.collect()

        allFeat = np.nan_to_num(
                np.concatenate(allFeat, axis = 1) )

        assert allFeat.shape[0] == uv_ids.shape[0]
        assert len(feat_names) == allFeat.shape[1], str(len(feat_names)) + " , " + str(allFeat.shape[1])

        # if we have excluded the ignore segments before, we need to reintroduce
        # them now to keep edge numbering consistent
        if self.has_seg_mask and not lifted_nh:
            where_ignore = np.logical_not( where_uv )
            n_ignore = np.sum(where_ignore)
            newFeat = np.zeros(
                    (allFeat.shape[0] + n_ignore, allFeat.shape[1]),
                    dtype = 'float32' )
            newFeat[where_uv] = allFeat
            allFeat = newFeat
            assert allFeat.shape[0] == uv_ids.shape[0] + n_ignore

        # save feature names
        save_folder = os.path.join(self.cache_folder, "features")
        if not os.path.exists(save_folder):
            os.mkdir(save_folder)
        save_file = os.path.join(save_folder,
            "region_features_" + str(seg_id) + "_" + str(inp_id) + "_" + str(lifted_nh) + ".h5" )
        vigra.writeHDF5(feat_names, save_file, "region_features_names")
        print "writing feat_names to", save_file

        return allFeat


    # get the names of the region features
    def region_features_names(self, seg_id, inp_id, lifted_nh):

        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert inp_id < self.n_inp, str(inp_id) + " , " + str(self.n_inp)
        self.region_features(seg_id, inp_id)

        save_folder = os.path.join(self.cache_folder, "features")
        save_file = os.path.join(save_folder,
            "region_features_" + str(seg_id) + "_" + str(inp_id) + "_" + str(lifted_nh) + ".h5" )
        assert os.path.exists(save_file)

        return vigra.readHDF5(save_file,"region_features_names")


    # Find the number of faces (= connected components), that make up the edge
    # Could find some more features based on the ccs
    @cacher_hdf5()
    def edge_connected_components(self, seg_id):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        rag = self._rag(seg_id)
        n_edges = rag.edgeNum

        n_ccs = np.zeros(n_edges)

        for edge_id in range( n_edges ):
            edge_coords = rag.edgeCoordinates(edge_id)
            # need to map grid graph coords to normal coords
            edge_coords = np.floor(edge_coords).astype(np.uint32)
            x_min = np.min(edge_coords[:,0])
            y_min = np.min(edge_coords[:,1])
            z_min = np.min(edge_coords[:,2])
            edge_coords[:,0] -= x_min
            edge_coords[:,1] -= y_min
            edge_coords[:,2] -= z_min
            x_max = np.max(edge_coords[:,0])
            y_max = np.max(edge_coords[:,1])
            z_max = np.max(edge_coords[:,2])
            edge_mask = np.zeros( (x_max + 1, y_max + 1, z_max + 1), dtype = np.uint32 )
            # bring edge_coords in np.where format
            edge_coords = (edge_coords[:,0], edge_coords[:,1], edge_coords[:,2])
            edge_mask[edge_coords] = 1
            ccs = vigra.analysis.labelVolumeWithBackground(edge_mask, neighborhood = 26)
            # - 1, because we have to substract for the background label
            n_ccs[edge_id] = len(np.unique(ccs)) - 1

        return n_ccs

    @cacher_hdf5()
    def node_z_coord(self, seg_id):
        rag = self._rag(seg_id)
        labels = rag.labels
        labels = labels.squeeze()
        nz = np.zeros(rag.maxNodeId +1, dtype='uint32')
        for z in range(labels.shape[2]):
            lz = labels[:,:,z]
            nz[lz] = z
        return nz


    # find the edge-type indications
    # 0 for z-edges, 1 for xy-edges
    @cacher_hdf5()
    def edge_indications(self, seg_id):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)

        # TODO test this and use instead
        # need to include some of the checks from below, too
        #node_z = self.node_z_coord(seg_id)
        #uv_ids = self._adjacent_segments(seg_id)
        #z_u = node_z[uv_ids[:,0]]
        #z_u = node_z[uv_ids[:,1]]
        ## xy edges (same z coordinate are) set to 1
        ## z  edges (different z coordinates) set to 0
        #return (z_u != z_v).astype('uint8')

        rag = self._rag(seg_id)
        n_edges = rag.edgeNum
        edge_indications = np.zeros(n_edges, dtype = 'uint8')
        uv_ids = rag.uvIds()

        # loop over the edges and check whether they are xy or z by checking the edge coords
        for edge_id in xrange(n_edges):
            edge_coords = rag.edgeCoordinates(edge_id)
            z = np.unique(edge_coords[:,2])
            if z.size > 1:
                uv = uv_ids[edge_id]
                if not 0 in uv:
                    assert False, "Edge indications can only be calculated for flat superpixel" + str(z)
                else:
                    continue
            z = z[0]
            # check whether we have a z (-> 0) or a xy edge (-> 1)
            edge_indications[edge_id] = 1 if (z - int(z) == 0.) else 0

        return edge_indications


    # TODO refactor the actual calculation to use the same code here and in defect ppl
    # Features from edge_topology
    @cacher_hdf5("feature_folder")
    def topology_features(self, seg_id, use_2d_edges):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert isinstance( use_2d_edges, bool ), type(use_2d_edges)

        if not use_2d_edges:
            n_feats = 1
        else:
            n_feats = 7

        rag = self._rag(seg_id)

        n_edges = rag.edgeNum
        topology_features = np.zeros( (n_edges, n_feats) )

        # length / area of the edge
        edge_lens = rag.edgeLengths()
        assert edge_lens.shape[0] == n_edges
        topology_features[:,0] = edge_lens
        topology_features_names = ["TopologyFeature_EdgeLength"]

        # deactivated for now, because it segfaults for large ds
        # TODO look into this
        ## number of connected components of the edge
        #n_ccs = self.edge_connected_components(seg_id)
        #assert n_ccs.shape[0] == n_edges
        #topology_features[:,1] = n_ccs
        #topology_features_names = ["TopologyFeature_NumFaces"]

        # extra feats for z-edges in 2,5 d
        if use_2d_edges:

            # edge indications
            edge_indications = self.edge_indications(seg_id)
            assert edge_indications.shape[0] == n_edges
            topology_features[:,1] = edge_indications
            topology_features_names.append("TopologyFeature_xy_vs_z_indication")

            # region sizes to build some features
            statistics =  [ "Count", "RegionCenter" ]

            extractor = vigra.analysis.extractRegionFeatures(
                    self.inp(0).astype(np.float32),
                    self.seg(seg_id).astype(np.uint32),
                    features = statistics )

            z_mask = edge_indications == 0

            sizes = extractor["Count"]
            uvIds = self._adjacent_segments(seg_id)
            sizes_u = sizes[ uvIds[:,0] ]
            sizes_v = sizes[ uvIds[:,1] ]
            # union = size_up + size_dn - intersect
            unions  = sizes_u + sizes_v - edge_lens
            # Union features
            topology_features[:,2][z_mask] = unions[z_mask]
            topology_features_names.append("TopologyFeature_union")
            # IoU features
            topology_features[:,3][z_mask] = edge_lens[z_mask] / unions[z_mask]
            topology_features_names.append("TopologyFeature_intersectionoverunion")

            # segment shape features
            seg_coordinates = extractor["RegionCenter"]
            len_bounds      = np.zeros(rag.nodeNum)
            # iterate over the nodes, to get the boundary length of each node
            for n in rag.nodeIter():
                node_z = seg_coordinates[n.id][2]
                for arc in rag.incEdgeIter(n):
                    edge = rag.edgeFromArc(arc)
                    edge_c = rag.edgeCoordinates(edge)
                    # only edges in the same slice!
                    if edge_c[0,2] == node_z:
                        len_bounds[n.id] += edge_lens[edge.id]
            # shape feature = Area / Circumference
            shape_feats_u = sizes_u / len_bounds[uvIds[:,0]]
            shape_feats_v = sizes_v / len_bounds[uvIds[:,1]]
            # combine w/ min, max, absdiff
            topology_features[:,4][z_mask] = np.minimum(
                    shape_feats_u[z_mask], shape_feats_v[z_mask])
            topology_features[:,5][z_mask] = np.maximum(
                    shape_feats_u[z_mask], shape_feats_v[z_mask])
            topology_features[:,6][z_mask] = np.absolute(
                    shape_feats_u[z_mask] - shape_feats_v[z_mask])
            topology_features_names.append("TopologyFeature_shapeSegment_min")
            topology_features_names.append("TopologyFeature_shapeSegment_max")
            topology_features_names.append("TopologyFeature_shapeSegment_absdiff")

            # edge shape features
            # this is too hacky, don't use it for now !
            #edge_bounds = np.zeros(rag.edgeNum)
            #adjacent_edges = self._adjacent_edges(seg_id)
            ## TODO no loop or CPP
            #for edge in rag.edgeIter():
            #    edge_coords = rag.edgeCoordinates(edge)
            #    edge_coords_up = np.ceil(edge_coords)
            #    #edge_coords_dn = np.floor(edge_coords)
            #    edge_z = edge_coords[0,2]
            #    for adj_edge_id in adjacent_edges[edge.id]:
            #        adj_coords = rag.edgeCoordinates(adj_edge_id)
            #        # only consider this edge, if it is in the same slice
            #        if adj_coords[0,2] == edge_z:
            #            # find the overlap and add it to the boundary
            #            #adj_coords_up = np.ceil(adj_coords)
            #            adj_coords_dn = np.floor(adj_coords)
            #            # overlaps (set magic...)
            #            ovlp0 = np.array(
            #                    [x for x in set(tuple(x) for x in edge_coords_up[:,:2])
            #                        & set(tuple(x) for x in adj_coords_dn[:,:2])] )
            #            #print edge_coords_up
            #            #print adj_coords_dn
            #            #print ovlp0
            #            #quit()
            #            #ovlp1 = np.array(
            #            #        [x for x in set(tuple(x) for x in edge_coords_dn[:,:2])
            #            #            & set(tuple(x) for x in adj_coords_up[:,:2])])
            #            #assert len(ovlp0) == len(ovlp1), str(len(ovlp0)) + " , " + str(len(ovlp1))
            #            edge_bounds[edge.id] += len(ovlp0)

            ## shape feature = Area / Circumference
            #topology_features[:,7][z_mask] = edge_lens[z_mask] / edge_bounds[z_mask]
            #topology_features_names.append("TopologyFeature_shapeEdge")

        save_folder = os.path.join(self.cache_folder, "features")
        if not os.path.exists(save_folder):
            os.mkdir(save_folder)
        save_name = "topology_features_" + str(seg_id) + "_" + str(use_2d_edges) + ".h5"
        save_file = os.path.join(save_folder, save_name )
        vigra.writeHDF5(topology_features_names, save_file, "topology_features_names")

        topology_features[np.isinf(topology_features)] = 0.
        topology_features[np.isneginf(topology_features)] = 0.
        topology_features = np.nan_to_num(topology_features)

        return topology_features


    # get the names of the region features
    def topology_features_names(self, seg_id, use_2d_edges):

        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        self.topology_features(seg_id, use_2d_edges)

        save_folder = os.path.join(self.cache_folder, "features")
        save_name = "topology_features_" + str(seg_id) + "_" + str(use_2d_edges) + ".h5"
        save_file = os.path.join(save_folder, save_name )
        assert os.path.exists(save_file)

        return vigra.readHDF5(save_file,"topology_features_names")


    # safely combine features
    # TODO loading the rag may consume some time and concatenate should also catch
    # non-matching shapes, so we could get rid of the asserts
    def combine_features(self, feat_list, seg_id):
        n_edges = self._rag(seg_id).edgeNum
        for f in feat_list:
            assert f.shape[0] == n_edges, str(f.shape[0]) + " , " +  str(n_edges)
        return np.concatenate(feat_list, axis = 1)


    # features based on curvature of xy edges
    # FIXME very naive implementation
    # FIXME this only works for sorted coordinates !!
    @cacher_hdf5("feature_folder")
    def curvature_features(self, seg_id):
        rag = self._rag(seg_id)
        curvature_feats = np.zeros( (rag.edgeNum, 4) )
        edge_ind = self.edge_indications(seg_id)
        for edge in xrange(rag.edgeNum):
            if edge_ind[edge] == 0:
                continue
            coords = rag.edgeCoordinates(edge)[:,:-1]
            try:
                dx_dt = np.gradient(coords[:,0])
            except IndexError as e:
                #print coords
                continue
            dy_dt = np.gradient(coords[:,1])
            d2x_dt2 = np.gradient(dx_dt)
            d2y_dt2 = np.gradient(dy_dt)

            # curvature implemented after:
            # http://stackoverflow.com/questions/28269379/curve-curvature-in-numpy
            curvature = np.abs(d2x_dt2 * dy_dt - dx_dt * d2y_dt2) / (dx_dt * dx_dt + dy_dt + dy_dt * dy_dt)**1.5

            curvature_feats[edge,0] = np.mean(curvature)
            curvature_feats[edge,1] = np.min(curvature)
            curvature_feats[edge,2] = np.max(curvature)
            curvature_feats[edge,3] = np.std(curvature)

        return np.nan_to_num(curvature_feats)


    #
    # Groundtruth projection
    #

    # get the edge labeling from dense groundtruth
    @cacher_hdf5()
    def edge_gt(self, seg_id):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert self.has_gt

        rag = self._rag(seg_id)
        node_gt, _ = rag.projectBaseGraphGt( self.gt().astype(np.uint32) )
        # this fails for non-consecutive gt, which however should not be a problem
        #assert node_gt.shape[0] == rag.nodeNum, str(node_gt.shape[0]) + " , " +  str(rag.nodeNum)

        uv_ids = self._adjacent_segments(seg_id)
        u_gt = node_gt[ uv_ids[:,0] ]
        v_gt = node_gt[ uv_ids[:,1] ]

        assert u_gt.shape == v_gt.shape
        assert u_gt.shape[0] == rag.edgeNum
        return (u_gt != v_gt).astype('uint8')


    # get edge gt from thresholding the overlaps
    # edges with ovlp > positive_threshold are taken as positive trainging examples
    # edges with ovlp < negative_threshold are taken as negative training examples
    @cacher_hdf5()
    def edge_gt_fuzzy(self, seg_id, positive_threshold, negative_threshold):
        assert positive_threshold > 0.5, str(positive_threshold)
        assert negative_threshold < 0.5, str(negative_threshold)
        print negative_threshold, positive_threshold
        edge_overlaps = self.edge_overlaps(seg_id)
        edge_gt_fuzzy = 0.5 * np.ones( edge_overlaps.shape )
        edge_gt_fuzzy[edge_overlaps > positive_threshold] = 1.
        edge_gt_fuzzy[edge_overlaps < negative_threshold] = 0.

        return edge_gt_fuzzy


    # get edge overlaps
    # with values from 0 (= no overlap) to 1 (= max overlap)
    @cacher_hdf5()
    def edge_overlaps(self, seg_id):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert self.has_gt

        edge_overlaps = agraph.candidateSegToRagSeg(
                self.seg(seg_id).astype('uint32'),
                self.gt().astype('uint32'),
                self._adjacent_segments(seg_id).astype(np.uint64))

        return edge_overlaps

    # return mask that hides edges that lie between 2 superpixel
    # which are projected to an ignore label
    # -> we don t want to learn on these!
    @cacher_hdf5(ignoreNumpyArrays=True)
    def ignore_mask(self, seg_id, uv_ids, with_defects = False): # with defects only  for caching
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert self.has_gt
        #need the node gt to determine the gt val of superpixel
        rag = self._rag(seg_id)
        node_gt, _ = rag.projectBaseGraphGt( self.gt().astype(np.uint32) )
        assert node_gt.shape[0] == rag.nodeNum, str(node_gt.shape[0]) + " , " +  str(rag.nodeNum)
        ignore_mask = np.zeros( rag.edgeNum, dtype = bool)
        for edge_id in xrange(rag.edgeNum):
            n0 = uv_ids[edge_id][0]
            n1 = uv_ids[edge_id][1]
            # if both superpixel have ignore label in the gt
            # block them in our mask
            if node_gt[n0] in self.gt_false_splits or node_gt[n1] in self.gt_false_splits:
                if node_gt[n0] != node_gt[n1]:
                    ignore_mask[edge_id] = True
            if node_gt[n0] in self.gt_false_merges and node_gt[n1] in self.gt_false_merges:
                ignore_mask[edge_id] = True

        print "IGNORE MASK NONZEROS:", np.sum(ignore_mask)
        return ignore_mask


    # return mask that hides edges that lie between 2 superpixel for lifted edges
    # which are projected to an ignore label
    # -> we don t want to learn on these!
    @cacher_hdf5(ignoreNumpyArrays=True)
    def lifted_ignore_mask(self, seg_id, liftedNh, liftedUvs, with_defects = False): # with defects only for caching
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert self.has_gt
        #need the node gt to determine the gt val of superpixel
        rag = self._rag(seg_id)
        node_gt, _ = rag.projectBaseGraphGt( self.gt().astype(np.uint32) )
        assert node_gt.shape[0] == rag.nodeNum, str(node_gt.shape[0]) + " , " +  str(rag.nodeNum)

        numEdges = liftedUvs.shape[0]

        ignore_mask = np.zeros( numEdges, dtype = bool)
        for edge_id in xrange(numEdges):
            n0 = liftedUvs[edge_id][0]
            n1 = liftedUvs[edge_id][1]
            # if both superpixel have ignore label in the gt
            # block them in our mask
            if node_gt[n0] in self.gt_false_splits or node_gt[n1] in self.gt_false_splits:
                if node_gt[n0] != node_gt[n1]:
                    ignore_mask[edge_id] = True
            if node_gt[n0] in self.gt_false_merges and node_gt[n1] in self.gt_false_merges:
                ignore_mask[edge_id] = True

        print "IGNORE MASK NONZEROS:", np.sum(ignore_mask)
        return ignore_mask


    # get the projection of the gt to the segmentation
    @cacher_hdf5()
    def seg_gt(self, seg_id):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        assert self.has_gt

        rag = self._rag(seg_id)
        seg = self.seg(seg_id)
        node_gt, _ = rag.projectBaseGraphGt( self.gt().astype(np.uint32) )
        assert node_gt.shape[0] == rag.nodeNum, str(node_gt.shape[0]) + " , " +  str(rag.nodeNum)

        seg_gt = rag.projectLabelsToBaseGraph(node_gt)
        assert seg_gt.shape == self.shape

        return seg_gt.astype(np.uint32)


    # get the projection of a multicut result to the segmentation
    def project_mc_result(self, seg_id, mc_node):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        rag = self._rag(seg_id)
        assert mc_node.shape[0] == rag.nodeNum, str(mc_node.shape[0]) + " , " + str(rag.nodeNum)

        mc_seg = rag.projectLabelsToBaseGraph(mc_node.astype(np.uint32))
        assert mc_seg.shape == self.shape

        return mc_seg.astype(np.uint32)


    #
    # Convenience functions for Cutouts and Tesselation
    #


    # make a cutout of the given block shape
    # need to update the ds in the MetaSet after this!
    def make_cutout(self, block_coordinates, block_offsets = [0,0,0]):
        assert self.has_raw, "Need at least raw data to make a cutout"
        assert len(block_coordinates) == 6
        assert block_coordinates[1] <= self.shape[0] and block_coordinates[3] <= self.shape[1] and block_coordinates[5] <= self.shape[2], str(block_coordinates) + " , " + str(self.shape)

        cutout_name = self.ds_name + "_cutout_" + str(self.n_cutouts)
        ancestor_folder = self.cache_folder
        if isinstance(self, Cutout):
            ancestor_folder = self.ancestor_folder
        cutout = Cutout(self.cache_folder, cutout_name, block_coordinates, ancestor_folder, block_offsets)

        # copy all inputs, segs and the gt to cutuout
        for inp in range(self.n_inp):
            inp_path = os.path.join(self.cache_folder,"inp" + str(inp) + ".h5")
            if isinstance(self, Cutout):
                inp_path = self.inp_path[inp]
            if inp == 0:
                cutout.add_raw(inp_path)
            else:
                cutout.add_input(inp_path)

        # check if we have a seg mask and copy
        if self.has_seg_mask:
            mask_path = os.path.join(self.cache_folder,"seg_mask.h5")
            cutout.add_seg_mask(mask_path, 'data')

        for seg_id in range(self.n_seg):
            seg_path = os.path.join(self.cache_folder,"seg" + str(seg_id) + ".h5")
            cutout.add_seg(seg_path, "data")

        if self.has_gt:
            gt_path = os.path.join(self.cache_folder,"gt.h5")
            cutout.add_gt(gt_path, "data")

        for false_merge_gt in self.gt_false_merges:
            cutout.add_false_merge_gt_id(false_merge_gt)

        for false_split_gt in self.gt_false_splits:
            cutout.add_false_split_gt_id(false_split_gt)

        # copy the defect slices
        def_slices = []
        for z in self.defect_slices:
            if (z >= block_coordinates[4] and z < block_coordinates[5]):
                def_slices.append(z - block_coordinates[4])
        cutout.add_defect_slices(def_slices)

        self.n_cutouts += 1
        self.cutouts.append(cutout)


    def get_cutout(self, cutout_id):
        assert cutout_id < self.n_cutouts, str(cutout_id) + " , " + str(self.n_cutouts)
        return self.cutouts[cutout_id]


    def make_inverse_cutout(self, cut_id):
        assert self.has_raw, "Need at least raw data to make a cutout"
        assert cut_id < self.n_cutouts, "Cutout was not done yet!"
        assert not cut_id in self.inverse_cutouts.keys(), "Inverse Cutout is already there!"
        inv_name = self.ds_name + "_invcut_" + str(cut_id)
        ancestor_folder = self.cache_folder
        if isinstance(self, Cutout):
            ancestor_folder = self.ancestor_folder

        cut_coordinates = self.get_cutout(cut_id).block_coordinates

        inv_cut = InverseCutout(self.cache_folder, inv_name,
                cut_id, cut_coordinates, self.shape, ancestor_folder)

        # copy all inputs, segs and the gt to the inverse cutout
        for inp in range(self.n_inp):
            inp_path = os.path.join(self.cache_folder,"inp" + str(inp) + ".h5")
            if inp == 0:
                inv_cut.add_raw(inp_path)
            else:
                inv_cut.add_input(inp_path)

        for seg_id in range(self.n_seg):
            seg_path = os.path.join(self.cache_folder,"seg" + str(seg_id) + ".h5")
            inv_cut.add_seg(seg_path)

        if self.has_gt:
            gt_path = os.path.join(self.cache_folder,"gt.h5")
            inv_cut.add_gt(gt_path)

        self.inverse_cutouts[cut_id] = inv_cut


    def get_inverse_cutout(self, cut_id):
        assert cut_id in self.inverse_cutouts.keys(), "InverseCutout not produced yet"
        return self.inverse_cutouts[cut_id]



#cutout from a given Dataset, used for cutouts and tesselations
#calls the cache of the parent dataset for inp, seg, gt and filtercalls the cache of the parent dataset for inp, seg, gt and filter
class Cutout(DataSet):

    def __init__(self, meta_folder, ds_name, block_coordinates, ancestor_folder, block_offsets):
        super(Cutout, self).__init__(meta_folder, ds_name)

        self.inp_path = []
        self.block_coordinates = block_coordinates
        self.shape = (self.block_coordinates[1] - self.block_coordinates[0],
                self.block_coordinates[3] - self.block_coordinates[2],
                self.block_coordinates[5] - self.block_coordinates[4])

        self.block_offsets = block_offsets

        # this is the cache folder of the "oldest ancestor",
        # i.e. of the top dataset that is not a cutout or tesselation
        # we need it for make_filters
        self.ancestor_folder = ancestor_folder

        self.bb = np.s_[
                self.block_coordinates[0]+self.block_offsets[0]:self.block_coordinates[1]+self.block_offsets[0],
                self.block_coordinates[2]+self.block_offsets[1]:self.block_coordinates[3]+self.block_offsets[1],
                self.block_coordinates[4]+self.block_offsets[2]:self.block_coordinates[5]+self.block_offsets[2]
                ]


        # fot the inputs, we dont need to cache everythin again, however for seg and gt we have to, because otherwise the segmentations are not consecutive any longer
    # add path to the raw data from original ds
    # expects hdf5 input
    def add_raw(self, raw_path):
        if self.has_raw:
            raise RuntimeError("Rawdata has already been added")
        assert os.path.exists(raw_path), raw_path
        with h5py.File(raw_path) as f:
            shape = f['data'].shape
        assert len(shape) == 3, "Only 3d data supported"
        # for subvolume make sure that boundaries are included
        p = self.block_coordinates
        assert shape[0] >= p[1] and shape[1] >= p[3] and shape[2] >= p[5]
        self.inp_path.append(raw_path)
        self.has_raw = True
        self.n_inp = 1


    # add path to input from original ds
    # expects hdf5 input
    def add_input(self, inp_path):
        if not self.has_raw:
            raise RuntimeError("Add Rawdata before additional pixmaps")
        with h5py.File(inp_path) as f:
            shape = f['data'].shape
        p = self.block_coordinates
        assert shape[0] >= p[1] and shape[1] >= p[3] and shape[2] >= p[5]
        self.inp_path.append(inp_path)
        self.n_inp += 1


    # return input with inp_id (0 corresponds to the raw data)
    def inp(self, inp_id):
        if inp_id >= self.n_inp:
            raise RuntimeError("Trying to read inp_id " + str(inp_id) + " but there are only " + str(self.n_inp) + " input maps")
        inp_path = self.inp_path[inp_id]
        with h5py.File(inp_path) as f:
            return f['data'][self.bb]


    # seg and gt can't be reimplemented that way, because they need to be connected!

    # we get the paths to the filters of the top dataset
    def make_filters(self,
            inp_id,
            anisotropy_factor,ancestor_folder,
            filter_names = [ "gaussianSmoothing",
                             "hessianOfGaussianEigenvalues",
                             "laplacianOfGaussian"],
            sigmas = [1.6, 4.2, 8.3]
            ):
        assert inp_id < self.n_inp, str(inp_id) + " , " + str(self.n_inp)
        assert anisotropy_factor >= 1., "Finer resolution in z-direction is nor supported"

        top_ds_folder = self.cache_folder
        # keep splitting the path, until we get to the meta folder
        # then we know, that we have reached the cache folder for the parent dataset
        while top_ds_folder != ancestor_folder:
            top_ds_folder, sub_folder = os.path.split(top_ds_folder)
        filter_folder = os.path.join(top_ds_folder, "filters")

        # determine, how we calculate the pixfeats (2d, pure 3d or 3d scaled with anisotropy)
        # save filters to corresponding path
        calculation_2d = False

        if anisotropy_factor == 1.:
            filter_folder = os.path.join(filter_folder, "filters_3d")
        elif anisotropy_factor >= self.aniso_max:
            filter_folder = os.path.join(filter_folder, "filters_2d")
            calculation_2d = True
        else:
            filter_folder = os.path.join(filter_folder, "filters_" + str(anisotropy_factor) )

        filter_folder = os.path.join(filter_folder,"inp_" + str(inp_id))

        assert os.path.exists(filter_folder), "Call make_filters of the parent DataSet, before calling it in the cutout!"

        # get all the files in the filter folder
        filter_paths = []
        for file in os.listdir(filter_folder):
            filter_paths.append( os.path.join(filter_folder,file) )

        # sort to make this consistent!
        filter_paths.sort()

        return filter_paths


# the inverse of a cutout
# implemented for crossvalidation mc style
# FIXME this is deprecated
# TODO this is pretty simple to implement with a seg mask now
class InverseCutout(Cutout):

    def __init__(self, meta_folder, inv_cut_name, cut_id,
            cut_coordinates, vol_shape, ancestor_folder):
        self.cut_id = cut_id
        self.cut_coordinates = cut_coordinates
        self.shape = vol_shape

        print "WARNNG InverseCutout is deprecated!"

        self.cache_folder = os.path.join(meta_folder, inv_cut_name)
        self.ds_name = inv_cut_name
        assert not os.path.exists(self.cache_folder), "This InverseCutout already exists"
        os.mkdir(self.cache_folder)
        self.ancestor_folder = ancestor_folder

        # have to set this to be consistent with top classes
        self.is_subvolume = False
        self.aniso_max = 20.
        self.compression = 'gzip'

        # we cant call the init of Cutout, so we have to redefinde these
        self.has_raw   = False
        self.n_inp     = 0
        self.inp_paths = []

        self.n_seg = 0
        self.has_gt = 0


    def add_raw(self, raw_path):
        assert not self.has_raw, "Rawdata has already been added!"
        with h5py.File(raw_path) as f:
            h5_ds = f["data"]
            shape = h5_ds.shape
        assert shape[0] >= self.shape[0] and shape[1] >= self.shape[1] and shape[2] >= self.shape[2]
        self.inp_paths.append(raw_path)
        self.n_inp = 1
        self.has_raw = True


    def add_input(self, inp_path):
        assert self.has_raw, "Add Rawdata first!"
        with h5py.File(inp_path) as f:
            h5_ds = f["data"]
            shape = h5_ds.shape
        assert shape[0] >= self.shape[0] and shape[1] >= self.shape[1] and shape[2] >= self.shape[2]
        self.inp_paths.append(inp_path)
        self.n_inp += 1


    def inp(self, inp_id):
        assert inp_id < self.n_inp, str(inp_id) + " , " + str(self.n_inp)
        inp = vigra.readHDF5(self.inp_paths[inp_id],"data")
        p = self.cut_coordinates
        inp[p[0]:p[1],p[2]:p[3],p[4]:p[5]] = 0
        return inp


    def add_seg(self, seg_path):
        assert self.has_raw, "Add Rawdata first!"
        seg = vigra.readHDF5(seg_path, "data")
        shape = seg.shape
        assert shape[0] >= self.shape[0] and shape[1] >= self.shape[1] and shape[2] >= self.shape[2]

        # zero  is reserved for the 'empty' part of the volume
        if 0 in seg:
            seg += 1

        p = self.cut_coordinates
        seg[p[0]:p[1],p[2]:p[3],p[4]:p[5]] = 0
        seg = vigra.analysis.labelVolume(seg)
        seg -= 1

        save_path = os.path.join(self.cache_folder, "seg" + str(self.n_seg) + ".h5")
        vigra.writeHDF5(seg, save_path, "data", compression = self.compression)
        self.n_seg += 1


    def seg(self, seg_id):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        save_path = os.path.join(self.cache_folder, "seg" + str(seg_id) + ".h5")
        return vigra.readHDF5(save_path, "data")


    def add_gt(self, gt_path):
        assert not self.has_gt, "GT already exists!"
        gt = vigra.readHDF5(gt_path, "data")
        assert gt.shape[0] >= self.shape[0] and gt.shape[1] >= self.shape[1] and gt.shape[2] >= self.shape[2]
        p = self.cut_coordinates
        gt[p[0]:p[1],p[2]:p[3],p[4]:p[5]] = 0
        gt = vigra.analysis.labelVolumeWithBackground(gt.astype(np.uint32))
        save_path = os.path.join(self.cache_folder, "gt" + ".h5")
        vigra.writeHDF5(gt, save_path, "data", compression = self.compression)
        self.has_gt = True


    def gt(self):
        assert self.has_gt
        save_path = os.path.join(self.cache_folder, "gt" + ".h5")
        gt = vigra.readHDF5(save_path, "data")
        return gt


    # returns ids of the edges that are artificially introduced
    # by the inverse cutout
    @cacher_hdf5()
    def get_artificial_edges(self,seg_id):
        assert seg_id < self.n_seg, str(seg_id) + " , " + str(self.n_seg)
        artificial_edge_ids = []
        uv_ids = self._adjacent_segments(seg_id)
        for edge_id in xrange(uv_ids.shape[0]):
            # the zero label is reserved for the region not covered by this inv cutout
            # so all edges linking to it are introduced by the cutout
            if uv_ids[edge_id,0] == 0 or uv_ids[edge_id,1] == 0:
                artificial_edge_ids.append(edge_id)
        return artificial_edge_ids


    # there's a bunch of methods, that cant be called
    # from inverse cutout, cause they don't make sense!
    def make_cutout(self, block_coordinates):
        raise AttributeError("Can't be called for InverseCutout")

    def get_cutout(self, cutout_id):
        raise AttributeError("Can't be called for InverseCutout")

    def make_inverse_cutout(self, block_coordinates):
        raise AttributeError("Can't be called for InverseCutout")

    def get_inverse_cutout(self, cutout_id):
        raise AttributeError("Can't be called for InverseCutout")
