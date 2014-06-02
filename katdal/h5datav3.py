"""Data accessor class for HDF5 files produced by RTS correlator."""

import logging

import numpy as np
import h5py
import katpoint

from .dataset import DataSet, WrongVersion, BrokenFile, Subarray, SpectralWindow, \
                     DEFAULT_SENSOR_PROPS, DEFAULT_VIRTUAL_SENSORS, _robust_target
from .sensordata import SensorData, SensorCache
from .categorical import CategoricalData, sensor_to_categorical
from .lazy_indexer import LazyIndexer, LazyTransform

logger = logging.getLogger(__name__)

# Simplify the scan activities to derive the basic state of the antenna (slewing, scanning, tracking, stopped)
SIMPLIFY_STATE = {'scan_ready': 'slew', 'scan': 'scan', 'scan_complete': 'scan', 'track': 'track', 'slew': 'slew'}

SENSOR_PROPS = dict(DEFAULT_SENSOR_PROPS)
SENSOR_PROPS.update({
    '*activity': {'greedy_values': ('slew', 'stop'), 'initial_value': 'slew',
                   'transform': lambda act: SIMPLIFY_STATE.get(act, 'stop')},
    '*target': {'initial_value': '', 'transform': _robust_target},
})

SENSOR_ALIASES = {
}


def _calc_azel(cache, name, ant):
    """Calculate virtual (az, el) sensors from actual ones in sensor cache."""
    real_sensor = 'Antennas/%s/%s' % (ant, 'pos_actual_scan_azim' if name.endswith('az') else 'pos_actual_scan_elev')
    cache[name] = sensor_data = katpoint.deg2rad(cache.get(real_sensor))
    return sensor_data

VIRTUAL_SENSORS = dict(DEFAULT_VIRTUAL_SENSORS)
VIRTUAL_SENSORS.update({'Antennas/{ant}/az': _calc_azel, 'Antennas/{ant}/el': _calc_azel})

FLAG_NAMES = ('reserved0', 'static', 'cam', 'reserved3', 'detected_rfi', 'predicted_rfi', 'reserved6', 'reserved7')
FLAG_DESCRIPTIONS = ('reserved - bit 0', 'predefined static flag list', 'flag based on live CAM information',
                     'reserved - bit 3', 'RFI detected in the online system', 'RFI predicted from space based pollutants',
                     'reserved - bit 6', 'reserved - bit 7')
WEIGHT_NAMES = ('precision',)
WEIGHT_DESCRIPTIONS = ('visibility precision (inverse variance, i.e. 1 / sigma^2)',)

#--------------------------------------------------------------------------------------------------
#--- Utility functions
#--------------------------------------------------------------------------------------------------

def dummy_dataset(name, shape, dtype, value):
    """Dummy HDF5 dataset containing a single value.

    This creates a dummy HDF5 dataset in memory containing a single value. It
    can have virtually unlimited size as the dataset is highly compressed.

    Parameters
    ----------
    name : string
        Name of dataset
    shape : sequence of int
        Shape of dataset
    dtype : :class:`numpy.dtype` object or equivalent
        Type of data stored in dataset
    value : object
        All elements in the dataset will equal this value

    Returns
    -------
    dataset : :class:`h5py.Dataset` object
        Dummy HDF5 dataset

    """
    # It is important to randomise the filename as h5py does not allow two writable file objects with the same name
    # Without this randomness katdal can only open one file requiring a dummy dataset
    random_string = ''.join(['%02x' % (x,) for x in np.random.randint(256, size=8)])
    dummy_file = h5py.File('%s_%s.h5' % (name, random_string), driver='core', backing_store=False)
    return dummy_file.create_dataset(name, shape=shape, maxshape=shape, dtype=dtype, fillvalue=value, compression='gzip')

#--------------------------------------------------------------------------------------------------
#--- CLASS :  H5DataV3
#--------------------------------------------------------------------------------------------------

class H5DataV3(DataSet):
    """Load HDF5 format version 3 file produced by RTS correlator.

    For more information on attributes, see the :class:`DataSet` docstring.

    Parameters
    ----------
    filename : string
        Name of HDF5 file
    ref_ant : string, optional
        Name of reference antenna, used to partition data set into scans
        (default is first antenna in use)
    time_offset : float, optional
        Offset to add to all correlator timestamps, in seconds
    kwargs : dict, optional
        Extra keyword arguments, typically meant for other formats and ignored

    Attributes
    ----------
    file : :class:`h5py.File` object
        Underlying HDF5 file, exposed via :mod:`h5py` interface

    """
    def __init__(self, filename, ref_ant='', time_offset=0.0, **kwargs):
        DataSet.__init__(self, filename, ref_ant, time_offset)

        # Load file
        self.file, self.version = H5DataV3._open(filename)
        f = self.file

        # Load main HDF5 groups
        data_group, tm_group = f['Data'], f['TelescopeModel']
        cbf_group = tm_group['cbf']

        # ------ Extract vis and timestamps ------

        self.dump_period = cbf_group.attrs['int_time']
        # Obtain visibilities and timestamps (load the latter explicitly, but obviously not the former...)
        self._vis = data_group['correlator_data']
        self._timestamps = data_group['timestamps'][:]
        num_dumps = len(self._timestamps)
        if num_dumps != self._vis.shape[0]:
            raise BrokenFile('Number of timestamps received from ingest '
                             '(%d) differs from number of dumps in data (%d)' % (num_dumps, self._vis.shape[0]))
        # Discard the last sample if the timestamp is a duplicate (caused by stop packet in k7_capture)
        num_dumps = (num_dumps - 1) if num_dumps > 1 and (self._timestamps[-1] == self._timestamps[-2]) else num_dumps
        self._timestamps = self._timestamps[:num_dumps]
        # The expected_dumps should always be an integer (like num_dumps), unless the timestamps and/or dump period
        # are messed up in the file, so the threshold of this test is a bit arbitrary (e.g. could use > 0.5)
        expected_dumps = (self._timestamps[-1] - self._timestamps[0]) / self.dump_period + 1
        if abs(expected_dumps - num_dumps) >= 0.01:
            # Warn the user, as this is anomalous
            logger.warning(("Irregular timestamps detected in file '%s': "
                           "expected %.3f dumps based on dump period and start/end times, got %d instead") %
                           (filename, expected_dumps, num_dumps))
        # Move timestamps from start of each dump to the middle of the dump
        self._timestamps += 0.5 * self.dump_period + self.time_offset
        if self._timestamps[0] < 1e9:
            logger.warning("File '%s' has invalid first correlator timestamp (%f)" % (filename, self._timestamps[0],))
        self._time_keep = np.ones(num_dumps, dtype=np.bool)
        self.start_time = katpoint.Timestamp(self._timestamps[0] - 0.5 * self.dump_period)
        self.end_time = katpoint.Timestamp(self._timestamps[-1] + 0.5 * self.dump_period)

        # ------ Extract flags ------

        # Check if flag group is present, else use dummy flag data
        self._flags = data_group['flags'] if 'flags' in data_group else \
                      dummy_dataset('dummy_flags', shape=self._vis.shape[:-1], dtype=np.uint8, value=0)
        # Obtain flag descriptions from file or recreate default flag description table
        self._flags_description = data_group['flags_description'] if 'flags_description' in data_group else \
                                  np.array(zip(FLAG_NAMES, FLAG_DESCRIPTIONS))

        # ------ Extract weights ------

        # check if weight group present, else use dummy weight data
        self._weights = data_group['weights'] if 'weights' in data_group else \
                        dummy_dataset('dummy_weights', shape=self._vis.shape[:-1] + (1,), dtype=np.float32, value=1.0)
        self._weights_description = np.array(zip(WEIGHT_NAMES, WEIGHT_DESCRIPTIONS))

        # ------ Extract sensors ------

        # Populate sensor cache with all HDF5 datasets below TelescopeModel group that fit the description of a sensor
        cache = {}
        def register_sensor(name, obj):
            """A sensor is defined as a non-empty dataset with expected dtype."""
            if isinstance(obj, h5py.Dataset) and obj.shape != () and obj.dtype.names == ('timestamp', 'value', 'status'):
                comp_name, sensor_name = name.split('/', 1)
                comp_type = tm_group[comp_name].attrs.get('class')
                # Mapping from specific components to generic sensor groups
                # Put antenna sensors in virtual Antenna group, the rest according to component type
                group_lookup = {'AntennaPositioner' : 'Antennas/' + comp_name}
                group_name = group_lookup.get(comp_type, comp_type) if comp_type else comp_name
                name = '/'.join((group_name, sensor_name))
                cache[name] = SensorData(obj, name)
        tm_group.visititems(register_sensor)
        self.sensor = SensorCache(cache, self._timestamps, self.dump_period, keep=self._time_keep,
                                  props=SENSOR_PROPS, virtual=VIRTUAL_SENSORS, aliases=SENSOR_ALIASES)

        # ------ Extract observation parameters ------

        self.obs_params = {}
        # Replay obs_params sensor and update obs_params dict accordingly
        obs_params = self.sensor.get('Observation/params', extract=False)['value']
        for obs_param in obs_params:
            key, val = obs_param.split(' ', 1)
            # Oh my goodness, use eval (at least suppress any context!)
            self.obs_params[key] = eval(val, {})
        # Get observation script parameters, with defaults
        self.observer = self.obs_params.get('observer', '')
        self.description = self.obs_params.get('description', '')
        self.experiment_id = self.obs_params.get('experiment_id', '')

        # ------ Extract subarrays ------

        # All antennas in configuration as katpoint Antenna objects
        ants = [katpoint.Antenna(tm_group[name].attrs['description']) for name in tm_group
                if tm_group[name].attrs.get('class') == 'AntennaPositioner']
        all_ants = [ant.name for ant in ants]
        # By default, only pick antennas that were in use by the script
        obs_ants = self.obs_params.get('ants')
        obs_ants = obs_ants.split(',') if obs_ants else all_ants
        self.ref_ant = obs_ants[0] if not ref_ant else ref_ant

        # Original list of correlation products as pairs of input labels
        corrprods = cbf_group.attrs['bls_ordering']
        
        if len(corrprods) != self._vis.shape[2]:
            # Apply k7_capture baseline mask after the fact, in the hope that it fixes correlation product mislabelling
            corrprods = np.array([cp for cp in corrprods if cp[0][:-1] in obs_ants and cp[1][:-1] in obs_ants])
            # If there is still a mismatch between labels and data shape, file is considered broken (maybe bad labels?)
            if len(corrprods) != self._vis.shape[2]:
                raise BrokenFile('Number of baseline labels (containing expected antenna names) '
                                 'received from correlator (%d) differs from number of baselines in data (%d)' %
                                 (len(corrprods), self._vis.shape[2]))
            else:
                logger.warning('Reapplied k7_capture baseline mask to fix unexpected number of baseline labels')
        self.subarrays = [Subarray(ants, corrprods)]
        self.sensor['Observation/subarray'] = CategoricalData(self.subarrays, [0, num_dumps])
        self.sensor['Observation/subarray_index'] = CategoricalData([0], [0, num_dumps])
        # Store antenna objects in sensor cache too, for use in virtual sensor calculations
        for ant in ants:
            self.sensor['Antennas/%s/antenna' % (ant.name,)] = CategoricalData([ant], [0, num_dumps])

        # ------ Extract spectral windows / frequencies ------

        # The centre frequency is now in the domain of the CBF
        centre_freq = cbf_group.attrs['center_freq']
        num_chans = cbf_group.attrs['n_chans']
        if num_chans != self._vis.shape[1]:
            raise BrokenFile('Number of channels received from correlator '
                             '(%d) differs from number of channels in data (%d)' % (num_chans, self._vis.shape[1]))
        bandwidth = cbf_group.attrs['bandwidth']
        channel_width = bandwidth / num_chans
        # The data product is set by the script or passed to it via schedule block
        product = self.obs_params.get('product', 'unknown')

        # We only expect a single spectral window within a single v3 file,
        # as changing the centre freq is like changing the CBF mode 
        self.spectral_windows = [SpectralWindow(centre_freq, channel_width, num_chans, product, sideband=1)]
        self.sensor['Observation/spw'] = CategoricalData(self.spectral_windows, [0, num_dumps])
        self.sensor['Observation/spw_index'] = CategoricalData([0], [0, num_dumps])

        # ------ Extract scans / compound scans / targets ------

        # Use the activity sensor of reference antenna to partition the data set into scans (and to set their states)
        scan = self.sensor.get('Antennas/%s/activity' % (self.ref_ant,))
        # If the antenna starts slewing on the second dump, incorporate the first dump into the slew too.
        # This scenario typically occurs when the first target is only set after the first dump is received.
        # The workaround avoids putting the first dump in a scan by itself, typically with an irrelevant target.
        if len(scan) > 1 and scan.events[1] == 1 and scan[1] == 'slew':
            scan.events, scan.indices = scan.events[1:], scan.indices[1:]
            scan.events[0] = 0
        # Use labels to partition the data set into compound scans
        label = self.sensor.get('Observation/label')
        # Discard empty labels (typically found in raster scans, where first scan has proper label and rest are empty)
        # However, if all labels are empty, keep them, otherwise whole data set will be one pathological compscan...
        if len(label.unique_values) > 1:
            label.remove('')
        # Create duplicate scan events where labels are set during a scan (i.e. not at start of scan)
        # ASSUMPTION: Number of scans >= number of labels (i.e. each label should introduce a new scan)
        scan.add_unmatched(label.events)
        self.sensor['Observation/scan_state'] = scan
        self.sensor['Observation/scan_index'] = CategoricalData(range(len(scan)), scan.events)
        # Move proper label events onto the nearest scan start
        # ASSUMPTION: Number of labels <= number of scans (i.e. only a single label allowed per scan)
        label.align(scan.events)
        # If one or more scans at start of data set have no corresponding label, add a default label for them
        if label.events[0] > 0:
            label.add(0, '')
        self.sensor['Observation/label'] = label
        self.sensor['Observation/compscan_index'] = CategoricalData(range(len(label)), label.events)
        # Use the target sensor of reference antenna to set the target for each scan
        target = self.sensor.get('Antennas/%s/target' % (self.ref_ant,))
        # Move target events onto the nearest scan start
        # ASSUMPTION: Number of targets <= number of scans (i.e. only a single target allowed per scan)
        target.align(scan.events)
        self.sensor['Observation/target'] = target
        self.sensor['Observation/target_index'] = CategoricalData(target.indices, target.events)
        # Set up catalogue containing all targets in file, with reference antenna as default antenna
        self.catalogue.add(target.unique_values)
        self.catalogue.antenna = 'Antennas/%s' % (self.ref_ant,)[0]

        # Avoid storing reference to self in transform closure below, as this hinders garbage collection
        dump_period, time_offset = self.dump_period, self.time_offset
        # Apply default selection and initialise all members that depend on selection in the process
        self.select(spw=0, subarray=0, ants=obs_ants)

    def __str__(self):
        """Verbose human-friendly string representation of data set."""
        descr = [super(H5DataV3, self).__str__()]
        # append the process_log, if it exists, for non-concatenated h5 files
        if 'process_log' in self.file['History']:
            descr.append('-------------------------------------------------------------------------------')
            descr.append('Process log:')
            for proc in self.file['History']['process_log']:
                param_list = '%15s:' % proc[0]
                for param in proc[1].split(','):
                    param_list += '  %s' % param
                descr.append(param_list)
        return '\n'.join(descr)

    @staticmethod
    def _open(filename):
        """Open file and do basic version sanity check."""
        f = h5py.File(filename, 'r')
        version = f.attrs.get('version', '1.x')
        if not version.startswith('3.'):
            raise WrongVersion("Attempting to load version '%s' file with version 3 loader" % (version,))
        return f, version

    @property
    def timestamps(self):
        """Visibility timestamps in UTC seconds since Unix epoch.

        The timestamps are returned as an array of float64, shape (*T*,),
        with one timestamp per integration aligned with the integration
        *midpoint*.

        """
        return self._timestamps[self._time_keep]

    @property
    def vis(self):
        """Complex visibility data as a function of time, frequency and baseline.

        The visibility data are returned as an array indexer of complex64, shape
        (*T*, *F*, *B*), with time along the first dimension, frequency along the
        second dimension and correlation product ("baseline") index along the
        third dimension. The returned array always has all three dimensions,
        even for scalar (single) values. The number of integrations *T* matches
        the length of :meth:`timestamps`, the number of frequency channels *F*
        matches the length of :meth:`freqs` and the number of correlation
        products *B* matches the length of :meth:`corr_products`. To get the
        data array itself from the indexer `x`, do `x[:]` or perform any other
        form of indexing on it. Only then will data be loaded into memory.

        """
        def _extract_vis(vis, keep):
            # Ensure that keep tuple has length of 3 (truncate or pad with blanket slices as necessary)
            keep = keep[:3] + (slice(None),) * (3 - len(keep))
            # Final indexing ensures that returned data are always 3-dimensional (i.e. keep singleton dimensions)
            # Discard the 4th / last dimension, however, as this is subsumed in the complex view of the data
            force_3dim = tuple([(np.newaxis if np.isscalar(dim_keep) else slice(None)) for dim_keep in keep] + [0])
            return vis.view(np.complex64)[force_3dim]
        extract_vis = LazyTransform('extract_vis', _extract_vis, lambda shape: shape[:-1], np.complex64)
        return LazyIndexer(self._vis, (self._time_keep, self._freq_keep, self._corrprod_keep),
                           transforms=[extract_vis])

    def weights(self, names=None):
        """Visibility weights as a function of time, frequency and baseline.

        Parameters
        ----------
        names : None or string or sequence of strings, optional
            List of names of weights to be multiplied together, as a sequence
            or string of comma-separated names (combine all weights by default)

        Returns
        -------
        weights : :class:`LazyIndexer` object of float32, shape (*T*, *F*, *B*)
            Array indexer with time along the first dimension, frequency along
            the second dimension and correlation product ("baseline") index
            along the third dimension. To get the data array itself from the
            indexer `x`, do `x[:]` or perform any other form of indexing on it.
            Only then will data be loaded into memory.

        """
        names = names.split(',') if isinstance(names, basestring) else WEIGHT_NAMES if names is None else names

        # Create index list for desired weights
        selection = []
        known_weights = [row[0] for row in self._weights_description]
        for name in names:
            try:
                selection.append(known_weights.index(name))
            except ValueError:
                logger.warning("'%s' is not a legitimate weight type for this file" % (name,))
        if not selection:
            logger.warning('No valid weights were selected - setting all weights to 1.0 by default')

        def _extract_weights(weights, keep):
            # Ensure that keep tuple has length of 3 (truncate or pad with blanket slices as necessary)
            keep = keep[:3] + (slice(None),) * (3 - len(keep))
            # Final indexing ensures that returned data are always 3-dimensional (i.e. keep singleton dimensions)
            force_3dim = tuple([(np.newaxis if np.isscalar(dim_keep) else slice(None)) for dim_keep in keep])
            # Multiply selected weights together (or select lone weight)
            # Strangely enough, if selection is [], prod produces the expected weights of 1.0 instead of an empty array
            return weights[force_3dim][:, :, :, selection[0]] if len(selection) == 1 else \
                   weights[force_3dim][:, :, :, selection].prod(axis=-1)
        extract_weights = LazyTransform('extract_weights', _extract_weights, lambda shape: shape[:-1], np.float32)
        return LazyIndexer(self._weights, (self._time_keep, self._freq_keep, self._corrprod_keep),
                           transforms=[extract_weights])

    def flags(self, names=None):
        """Flags as a function of time, frequency and baseline.

        Parameters
        ----------
        names : None or string or sequence of strings, optional
            List of names of flags that will be OR'ed together, as a sequence or
            a string of comma-separated names (use all flags by default)

        Returns
        -------
        flags : :class:`LazyIndexer` object of bool, shape (*T*, *F*, *B*)
            Array indexer with time along the first dimension, frequency along
            the second dimension and correlation product ("baseline") index
            along the third dimension. To get the data array itself from the
            indexer `x`, do `x[:]` or perform any other form of indexing on it.
            Only then will data be loaded into memory.

        """
        names = names.split(',') if isinstance(names, basestring) else FLAG_NAMES if names is None else names

        # Create index list for desired flags
        flagmask = np.zeros(8, dtype=np.int)
        known_flags = [row[0] for row in self._flags_description]
        for name in names:
            try:
                flagmask[known_flags.index(name)] = 1
            except ValueError:
                logger.warning("'%s' is not a legitimate flag type for this file" % (name,))
        # Pack index list into bit mask
        flagmask = np.packbits(flagmask)
        if not flagmask:
            logger.warning('No valid flags were selected - setting all flags to False by default')

        def _extract_flags(flags, keep):
            # Ensure that keep tuple has length of 3 (truncate or pad with blanket slices as necessary)
            keep = keep[:3] + (slice(None),) * (3 - len(keep))
            # Final indexing ensures that returned data are always 3-dimensional (i.e. keep singleton dimensions)
            force_3dim = tuple([(np.newaxis if np.isscalar(dim_keep) else slice(None)) for dim_keep in keep])
            flags_3dim = flags[force_3dim]
            # Use flagmask to blank out the flags we don't want
            total_flags = np.bitwise_and(flagmask, flags_3dim)
            # Convert uint8 to bool: if any flag bits set, flag is set
            return np.bool_(total_flags)
        extract_flags = LazyTransform('extract_flags', _extract_flags, dtype=np.bool)
        return LazyIndexer(self._flags, (self._time_keep, self._freq_keep, self._corrprod_keep),
                           transforms=[extract_flags])

    @staticmethod
    def _get_ants(filename):
        """Quick look function to get a list of antennas in a file.

        This is intended to be called without creating a complete katdal object.

        Parameters
        ----------
        filename : string
            Data file name

        Returns
        -------
        antennas : list of :class:'katpoint.Antenna' objects

        """
        f, version = H5DataV3._open(filename)
        tm_group = f['TelescopeModel']
        antennas = [katpoint.Antenna(tm_group[name].attrs['description'])
                    for name in tm_group
                    if tm_group[name].attrs.get('class') == 'AntennaPositioner']
        return antennas

    @staticmethod
    def _get_targets(filename):
        """Quick look function to get a list of targets in a file.

        This is intended to be called without creating a complete katdal object.

        Parameters
        ----------
        filename : string
            Data file name

        Returns
        -------
        targets : list of :class:'katpoint.Target' objects

        """
        f, version = H5DataV3._open(filename)
        target_list = f['TelescopeModel/cbf/target']
        all_target_strings = [target_data[1] for target_data in target_list]
        targets = [katpoint.Target(target_string)
                   for target_string in np.unique(all_target_strings)]
        return targets
