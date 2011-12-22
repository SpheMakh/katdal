"""Data accessor class for HDF5 files produced by Fringe Finder correlator."""

import logging
import re

import numpy as np
import h5py
import katpoint

from .dataset import DataSet, WrongVersion, BrokenFile, Subarray, SpectralWindow, \
                     DEFAULT_SENSOR_PROPS, DEFAULT_VIRTUAL_SENSORS, _robust_target
from .sensordata import SensorData, SensorCache
from .categorical import CategoricalData
from .lazy_indexer import LazyIndexer
from .concatdata import ConcatenatedLazyIndexer

logger = logging.getLogger('katfile.h5datav1')

def _labels_to_state(scan_label, compscan_label):
    """Use scan and compscan labels to derive basic state of antenna."""
    if not scan_label or scan_label == 'slew':
        return 'slew'
    if scan_label == 'cal':
        return 'track'
    return 'track' if compscan_label == 'track' else 'scan'

SENSOR_PROPS = dict(DEFAULT_SENSOR_PROPS)

SENSOR_ALIASES = {
    'nd_coupler' : 'rfe3_rfe15_noise_coupler_on',
    'nd_pin' : 'rfe3_rfe15_noise_pin_on',
}

def _calc_azel(cache, name, ant):
    """Calculate virtual (az, el) sensors from actual ones in sensor cache."""
    real_sensor = 'Antennas/%s/%s' % (ant, 'pos_actual_scan_azim' if name.endswith('az') else 'pos_actual_scan_elev')
    cache[name] = sensor_data = katpoint.deg2rad(cache.get(real_sensor))
    return sensor_data

VIRTUAL_SENSORS = dict(DEFAULT_VIRTUAL_SENSORS)
VIRTUAL_SENSORS.update({'Antennas/{ant}/az' : _calc_azel, 'Antennas/{ant}/el' : _calc_azel})

#--------------------------------------------------------------------------------------------------
#--- CLASS :  H5DataV1
#--------------------------------------------------------------------------------------------------

class H5DataV1(DataSet):
    """Load HDF5 format version 1 file produced by Fringe Finder correlator.

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

    Attributes
    ----------
    file : :class:`h5py.File` object
        Underlying HDF5 file, exposed via :mod:`h5py` interface

    """
    def __init__(self, filename, ref_ant='', time_offset=0.0):
        DataSet.__init__(self, filename, ref_ant, time_offset)

        # Load file
        self.file = f = h5py.File(filename, 'r')

        # Only continue if file is correct version and has been properly augmented
        self.version = f.attrs.get('version', '1.x')
        if not self.version.startswith('1.'):
            raise WrongVersion("Attempting to load version '%s' file with version 1 loader" % (self.version,))
        if not 'augment' in f.attrs:
            raise BrokenFile('HDF5 file not augmented - please run augment4.py (provided by k7augment package)')

        # Load main HDF5 groups
        ants_group, corr_group, data_group = f['Antennas'], f['Correlator'], f['Scans']
        # Get observation script attributes, with defaults
        self.observer = f.attrs.get('observer', '')
        self.description = f.attrs.get('description', '')
        self.experiment_id = f.attrs.get('experiment_id', '')

        # Collect all groups below data group that fit the description of a scan group
        scan_groups = []
        def register_scan_group(name, obj):
            """A scan group is defined as a group named 'Scan*' with non-empty timestamps and data."""
            if isinstance(obj, h5py.Group) and name.split('/')[-1].startswith('Scan') and \
               'data' in obj and 'timestamps' in obj and len(obj['timestamps']) > 0:
                scan_groups.append(obj)
        data_group.visititems(register_scan_group)
        # Sort scan groups in chronological order via 'decorate-sort-undecorate' (DSU) idiom
        decorated_scan_groups = [(s['timestamps'][0], s) for s in scan_groups]
        decorated_scan_groups.sort()
        self._scan_groups = [s[-1] for s in decorated_scan_groups]

        # ------ Extract timestamps ------

        self.dump_period = 1.0 / corr_group.attrs['dump_rate_hz']
        self._segments = np.cumsum([0] + [len(s['timestamps']) for s in self._scan_groups])
        num_dumps = self._segments[-1]
        self._time_keep = np.ones(num_dumps, dtype=np.bool)
        data_timestamps = self.timestamps
        if data_timestamps[0] < 1e9:
            logger.warning("File '%s' has invalid first correlator timestamp (%f)" % (filename, data_timestamps[0],))
        # Estimate timestamps by assuming they are uniformly spaced (much quicker than loading them from file).
        # This is useful for the purpose of segmenting data set, where accurate timestamps are not that crucial.
        # The real timestamps are still loaded when the user explicitly asks for them.
        # Do quick test for uniform spacing of timestamps (necessary but not sufficient).
        if abs((data_timestamps[-1] - data_timestamps[0]) / self.dump_period  + 1 - num_dumps) < 0.01:
            # Estimate the timestamps as being uniformly spaced
            data_timestamps = data_timestamps[0] + self.dump_period * np.arange(num_dumps)
        else:
            # Load the real timestamps instead and warn the user, as this is anomalous
            data_timestamps = data_timestamps[:]
            expected_dumps = (data_timestamps[-1] - data_timestamps[0]) / self.dump_period + 1
            logger.warning(("Irregular timestamps detected in file '%s':"
                            "expected %.3f dumps based on dump period and start/end times, got %d instead") %
                           (filename, expected_dumps, num_dumps))
        self.start_time = katpoint.Timestamp(data_timestamps[0] - 0.5 * self.dump_period)
        self.end_time = katpoint.Timestamp(data_timestamps[-1] + 0.5 * self.dump_period)

        # ------ Extract sensors ------

        # Populate sensor cache with all HDF5 datasets below antennas group that fit the description of a sensor
        cache = {}
        def register_sensor(name, obj):
            if isinstance(obj, h5py.Dataset) and obj.shape != () and obj.dtype.names == ('timestamp','value','status'):
                # Assume sensor dataset name is AntennaN/Sensors/dataset and rename it to Antennas/{ant}/dataset
                ant_name = obj.parent.parent.attrs['description'].split(',')[0]
                standardised_name = 'Antennas/%s/%s' % (ant_name, name.split('/')[-1])
                cache[standardised_name] = SensorData(obj, standardised_name)
        ants_group.visititems(register_sensor)
        # Use estimated data timestamps for now, to speed up data segmentation
        # This will linearly interpolate pointing coordinates to correlator data timestamps (on access)
        # As long as azimuth is in natural antenna coordinates, no special angle interpolation required
        self.sensor = SensorCache(cache, data_timestamps, self.dump_period, keep=self._time_keep,
                                  props=SENSOR_PROPS, virtual=VIRTUAL_SENSORS, aliases=SENSOR_ALIASES)

        # ------ Extract subarrays ------

        ants = [katpoint.Antenna(ants_group[group].attrs['description']) for group in ants_group]
        self.ref_ant = ants[0].name if not ref_ant else ref_ant
        # Map from (old-style) DBE input label (e.g. '0x') to the new antenna-based input label (e.g. 'ant1h')
        input_label = dict([(ants_group[group]['H'].attrs['dbe_input'], ant.name + 'h')
                            for ant, group in zip(ants, ants_group.keys()) if 'H' in ants_group[group]])
        input_label.update(dict([(ants_group[group]['V'].attrs['dbe_input'], ant.name + 'v')
                                 for ant, group in zip(ants, ants_group.keys()) if 'V' in ants_group[group]]))
        # Split DBE input product string into its separate inputs
        split_product = re.compile(r'(\d+[xy])(\d+[xy])')
        # Iterate over map from correlation product index to DBE input product string and convert
        # the latter to pairs of input labels (this assumes that the corrprod indices are sorted)
        corrprods = []
        for corrind, product in corr_group['input_map']:
            match = split_product.match(product)
            if match is None:
                raise BrokenFile("Unknown DBE input product '%s' in input map (expected e.g. '0x1y')" % (product,))
            corrprods.append(tuple([input_label[inp] for inp in match.groups()]))
        data_cp_len = len(self._scan_groups[0]['data'].dtype)
        if len(corrprods) != data_cp_len:
            raise BrokenFile('Number of baseline labels received from correlator '
                             '(%d) differs from number of baselines in data (%d)' % (len(corrprods), data_cp_len))
        self.subarrays = [Subarray(ants, corrprods)]
        self.sensor['Observation/subarray'] = CategoricalData(self.subarrays, [0, len(data_timestamps)])
        self.sensor['Observation/subarray_index'] = CategoricalData([0], [0, len(data_timestamps)])
        # Store antenna objects in sensor cache too, for use in virtual sensor calculations
        for ant in ants:
            self.sensor['Antennas/%s/antenna' % (ant.name,)] = CategoricalData([ant], [0, len(data_timestamps)])

        # ------ Extract spectral windows / frequencies ------

        centre_freq = corr_group.attrs['center_frequency_hz']
        num_chans = corr_group.attrs['num_freq_channels']
        data_num_chans = self._scan_groups[0]['data'].shape[1]
        if num_chans != data_num_chans:
            raise BrokenFile('Number of channels received from correlator '
                             '(%d) differs from number of channels in data (%d)' % (num_chans, data_num_chans))
        channel_width = corr_group.attrs['channel_bandwidth_hz']
        self.spectral_windows = [SpectralWindow(centre_freq, channel_width, num_chans)]
        self.sensor['Observation/spw'] = CategoricalData(self.spectral_windows, [0, len(data_timestamps)])
        self.sensor['Observation/spw_index'] = CategoricalData([0], [0, len(data_timestamps)])

        # ------ Extract scans / compound scans / targets ------

        # Fringe Finder augment does not store antenna activity sensors - use scan + compscan labels as a guess
        scan_labels = [s.attrs.get('label', '') for s in self._scan_groups]
        compscan_labels = [s.parent.attrs.get('label', '') for s in self._scan_groups]
        scan_states = [_labels_to_state(s, cs) for s, cs in zip(scan_labels, compscan_labels)]
        # The scans are already partitioned into groups - use corresponding segments as start events
        self.sensor['Observation/scan_state'] = CategoricalData(scan_states, self._segments)
        self.sensor['Observation/scan_index'] = CategoricalData(range(len(scan_states)), self._segments)
        # Group scans together based on compscan group name and have one label per compound scan
        compscan = CategoricalData([s.parent.name for s in self._scan_groups], self._segments)
        compscan.remove_repeats()
        label = CategoricalData(compscan_labels, self._segments)
        label.align(compscan.events)
        self.sensor['Observation/label'] = label
        self.sensor['Observation/compscan_index'] = CategoricalData(range(len(label)), label.events)
        # Extract targets from compscan groups, replacing empty or bad descriptions with dummy target
        target = CategoricalData([_robust_target(s.parent.attrs.get('target', ''))
                                  for s in self._scan_groups], self._segments)
        target.align(compscan.events)
        self.sensor['Observation/target'] = target
        self.sensor['Observation/target_index'] = CategoricalData(target.indices, target.events)
        # Set up catalogue containing all targets in file, with reference antenna as default antenna
        self.catalogue.add(target.unique_values)
        self.catalogue.antenna = self.sensor['Antennas/%s/antenna' % (self.ref_ant,)][0]

        # Restore original (slow) timestamps so that subsequent sensors (e.g. pointing) will have accurate values
        self.sensor.timestamps = self.timestamps
        # Apply default selection and initialise all members that depend on selection in the process
        self.select(spw=0, subarray=0)

    @property
    def timestamps(self):
        """Visibility timestamps in UTC seconds since epoch.

        The timestamps are returned as an array indexer of float64, shape (*T*,),
        with one timestamp per integration aligned with the integration
        *midpoint*. To get the data array itself from the indexer `x`, do `x[:]`
        or perform any other form of indexing on it.

        """
        indexers = []
        # Avoid storing reference to self in extract_time closure below, as this hinders garbage collection
        dump_period, time_offset = self.dump_period, self.time_offset
        # Convert from millisecs to secs since Unix epoch, and be sure to use float64 to preserve digits
        extract_time = lambda t, keep: np.float64(t) / 1000. + 0.5 * dump_period + time_offset
        for n, s in enumerate(self._scan_groups):
            indexers.append(LazyIndexer(s['timestamps'], keep=self._time_keep[self._segments[n]:self._segments[n + 1]],
                                        transform=extract_time, dtype=np.float64))
        return ConcatenatedLazyIndexer(indexers)

    @property
    def vis(self):
        """Complex visibility data as a function of time, frequency and baseline.

        The visibility data is returned as an array indexer of complex64, shape
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
        # Fringe Finder has a weird vis data structure: each scan data group is a recarray
        # with shape (T, F) and fields '0'...'11' indicating the correlation products.
        # The per-scan LazyIndexers therefore only do the time + frequency indexing,
        # leaving corrprod indexing to the final transform.
        indexers = []
        corrprod_keep = self._corrprod_keep
        # Apply both first-stage and second-stage corrprod indexing in the transform
        def index_corrprod(tf, keep):
            same_ndim = tuple([(np.newaxis if np.isscalar(dim_keep) else slice(None)) for dim_keep in keep[:3]])
            return np.dstack([tf[str(corrind)][same_ndim[:2]] for corrind in np.nonzero(corrprod_keep)[0]])\
                   [:, :, keep[2] if len(keep) > 2 else slice(None)][:, :, same_ndim[2]]
        for n, s in enumerate(self._scan_groups):
            indexers.append(LazyIndexer(s['data'], keep=(self._time_keep[self._segments[n]:self._segments[n + 1]],
                                                         self._freq_keep),
                                        transform=index_corrprod,
                                        shape_transform=lambda shape: (shape[0], shape[1], corrprod_keep.sum()),
                                        dtype=np.complex64))
        return ConcatenatedLazyIndexer(indexers)
