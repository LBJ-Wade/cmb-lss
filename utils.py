import logging
import math

import numpy as np
from astropy.table import Table
import healpy as hp
import pymaster as nmt
import pyccl as ccl
import yaml

logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', datefmt='%d/%m/%Y %H:%M:%S', level=logging.INFO)
logger = logging.getLogger(__name__)


class struct(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class ISWTracer(ccl.Tracer):
    """Specific :class:`Tracer` associated with the integrated Sachs-Wolfe
    effect (ISW). Useful when cross-correlating any low-redshift probe with
    the primary CMB anisotropies. The ISW contribution to the temperature
    fluctuations is:

    .. math::
       \\frac{\\Delta T}{T} = 2\\int_0^{\\chi_{LSS}}d\\chi a\\,\\dot{\\phi}

    Any angular power spectra computed with this tracer, should use
    a three-dimensional power spectrum involving the matter power spectrum.
    The current implementation of this tracers assumes a standard Poisson
    equation relating :math:`\\phi` and :math:`\\delta`, and linear structure
    growth. Although this should be valid  in :math:`\\Lambda`CDM and on
    the large scales the ISW is sensitive to, these approximations must be
    borne in mind.

    Args:
        cosmo (:class:`~pyccl.core.Cosmology`): Cosmology object.
        zmax (float): maximum redshift up to which we define the
            kernel.
        n_chi (float): number of intervals in the radial comoving
            distance on which we sample the kernel.
    """

    def __init__(self, cosmo, z_max=6., n_chi=1024):
        self.chi_max = ccl.comoving_radial_distance(cosmo, 1. / (1 + z_max))
        chi = np.linspace(0, self.chi_max, n_chi)
        a_arr = ccl.scale_factor_of_chi(cosmo, chi)
        H0 = cosmo['h'] / ccl.physical_constants.CLIGHT_HMPC
        OM = cosmo['Omega_c'] + cosmo['Omega_b']
        Ez = ccl.h_over_h0(cosmo, a_arr)
        fz = ccl.growth_rate(cosmo, a_arr)
        w_arr = 3 * cosmo['T_CMB'] * H0 ** 3 * OM * Ez * chi ** 2 * (1 - fz)

        self._trc = []
        self.add_tracer(cosmo, kernel=(chi, w_arr), der_bessel=-1)


def get_chi_squared(data_vector, model_vector, covariance_matrix):
    inverted_covariance = np.linalg.inv(covariance_matrix)
    diff = data_vector - model_vector
    return diff.dot(inverted_covariance).dot(diff)


def compute_master(field_a, field_b, binning):
    workspace = nmt.NmtWorkspace()
    workspace.compute_coupling_matrix(field_a, field_b, binning)
    cl_coupled = nmt.compute_coupled_cell(field_a, field_b)
    cl_decoupled = workspace.decouple_cell(cl_coupled)
    return cl_decoupled[0], workspace


def get_correlation_matrix(covariance_matrix):
    correlation_matrix = covariance_matrix.copy()
    for i in range(covariance_matrix.shape[0]):
        for j in range(covariance_matrix.shape[1]):
            correlation_matrix[i, j] = covariance_matrix[i, j] / math.sqrt(
                covariance_matrix[i, i] * covariance_matrix[j, j])
    return correlation_matrix


def get_overdensity_map(counts_map, mask, noise_weight_map=None):
    noise_weight_map = np.ones(len(counts_map)) if noise_weight_map is None else noise_weight_map
    # TODO: should be weighted by values in mask?
    sky_mean = (counts_map / noise_weight_map).mean()
    overdensity_map = counts_map / noise_weight_map / sky_mean - 1
    overdensity_map = get_masked_map(overdensity_map, mask)
    return overdensity_map


def get_shot_noise(map, mask):
    sky_frac = np.sum(mask) / np.shape(mask)[0]
    # TODO: not only non zero but weighted mean?
    n_obj = np.sum(map[np.nonzero(mask)])
    shot_noise = 4.0 * math.pi * sky_frac / n_obj
    return shot_noise


def add_mask(map, additional_mask):
    map = map.copy()
    map.mask = np.logical_or(map.mask, np.logical_not(additional_mask))
    return map


def get_masked_map(map, mask):
    map = hp.ma(map)
    map.mask = np.logical_not(mask)
    return map


def tansform_map_and_mask_to_nside(map, mask, nside):
    if nside:
        map = hp.pixelfunc.ud_grade(map, nside)
        mask = hp.pixelfunc.ud_grade(mask, nside)
    return map, mask


def get_mean_map(l, b, v, nside):
    thetas, phis = np.radians(-b + 90.), np.radians(l)
    npix = hp.nside2npix(nside)  # 12 * nside ^ 2
    n_obj_map = np.zeros(npix, dtype=np.float)
    mean_map = np.zeros(npix, dtype=np.float)

    indices = hp.ang2pix(nside, thetas, phis, nest=False)
    for i, j in enumerate(indices):
        # Add objects weight and store a count
        mean_map[j] += v[i]
        n_obj_map[j] += 1
    mean_map /= n_obj_map

    return mean_map


def get_redshift_distribution(data, n_bins=50, z_col='Z_PHOTO_QSO'):
    n_arr, z_arr = np.histogram(data[z_col], bins=n_bins)
    z_arr = np.array([(z_arr[i + 1] + z_arr[i]) / 2 for i in range(len(z_arr) - 1)])
    return z_arr, n_arr


def get_map(l, b, nside=128):
    phis, thetas = np.radians(l), np.radians(-b + 90.)
    n_pix = hp.nside2npix(nside)
    pixel_indices = hp.ang2pix(nside, thetas, phis)
    map_n = np.bincount(pixel_indices, minlength=n_pix)
    map_n = map_n.astype(float)
    return map_n


def read_fits_to_pandas(filepath, columns=None, n=None):
    table = Table.read(filepath, format='fits')

    # Get first n rows if limit specified
    if n:
        table = table[0:n]

    # Get proper columns into a pandas data frame
    if columns:
        table = table[columns]
    table = table.to_pandas()

    # Astropy table assumes strings are byte arrays
    for col in ['ID', 'ID_1', 'CLASS', 'CLASS_PHOTO', 'id1']:
        if col in table and hasattr(table.loc[0, col], 'decode'):
            table.loc[:, col] = table[col].apply(lambda x: x.decode('UTF-8').strip())

    # Change type to work with it as with a bit map
    if 'IMAFLAGS_ISO' in table:
        table.loc[:, 'IMAFLAGS_ISO'] = table['IMAFLAGS_ISO'].astype(int)

    return table


def get_pairs(values_arr, join_with=''):
    return [join_with.join(sorted([a, b])) for i, a in enumerate(values_arr) for b in values_arr[i:]]


def get_config(config_name):
    with open('../configs.yml', 'r') as config_file:
        config = yaml.full_load(config_file)
    config = config[config_name]
    return config
