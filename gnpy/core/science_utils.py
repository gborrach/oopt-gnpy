import numpy as np
from operator import attrgetter
from logging import getLogger
import scipy.constants as ph
from scipy.integrate import solve_bvp
from scipy.integrate import cumtrapz
from scipy.interpolate import interp1d
from scipy.optimize import OptimizeResult
from gnpy.core.utils import db2lin
from copy import deepcopy
from gnpy.core.parameters import SimParams


logger = getLogger(__name__)


def propagate_raman_fiber(fiber, *carriers):
    simulation = Simulation.get_simulation()
    sim_params = simulation.sim_params
    raman_params = sim_params.raman_params
    nli_params = sim_params.nli_params
    # apply input attenuation to carriers
    attenuation_in = db2lin(fiber.params.con_in + fiber.params.att_in)
    chan = []
    for carrier in carriers:
        pwr = carrier.power
        pwr = pwr._replace(signal=pwr.signal / attenuation_in,
                           nli=pwr.nli / attenuation_in,
                           ase=pwr.ase / attenuation_in)
        carrier = carrier._replace(power=pwr)
        chan.append(carrier)
    carriers = tuple(f for f in chan)

    # evaluate fiber attenuation involving also SRS if required by sim_params
    raman_solver = fiber.raman_solver
    raman_solver.carriers = carriers
    raman_solver.raman_pumps = fiber.raman_pumps
    stimulated_raman_scattering = raman_solver.stimulated_raman_scattering

    fiber_attenuation = (stimulated_raman_scattering.rho[:, -1])**-2
    if not raman_params.flag_raman:
        fiber_attenuation = tuple(fiber.params.lin_attenuation for _ in carriers)

    # evaluate Raman ASE noise if required by sim_params and if raman pumps are present
    if raman_params.flag_raman and fiber.raman_pumps:
        raman_ase = raman_solver.spontaneous_raman_scattering.power[:, -1]
    else:
        raman_ase = tuple(0 for _ in carriers)

    # evaluate nli and propagate in fiber
    attenuation_out = db2lin(fiber.params.con_out)
    nli_solver = fiber.nli_solver
    nli_solver.stimulated_raman_scattering = stimulated_raman_scattering

    nli_frequencies = []
    computed_nli = []
    for carrier in (c for c in carriers if c.channel_number in sim_params.nli_params.computed_channels):
        resolution_param = frequency_resolution(carrier, carriers, sim_params, fiber)
        f_cut_resolution, f_pump_resolution, _, _ = resolution_param
        nli_params.f_cut_resolution = f_cut_resolution
        nli_params.f_pump_resolution = f_pump_resolution
        nli_frequencies.append(carrier.frequency)
        computed_nli.append(nli_solver.compute_nli(carrier, *carriers))

    new_carriers = []
    for carrier, attenuation, rmn_ase in zip(carriers, fiber_attenuation, raman_ase):
        carrier_nli = np.interp(carrier.frequency, nli_frequencies, computed_nli)
        pwr = carrier.power
        pwr = pwr._replace(signal=pwr.signal/attenuation/attenuation_out,
                           nli=(pwr.nli+carrier_nli)/attenuation/attenuation_out,
                           ase=((pwr.ase/attenuation)+rmn_ase)/attenuation_out)
        new_carriers.append(carrier._replace(power=pwr))
    return new_carriers


def frequency_resolution(carrier, carriers, sim_params, fiber):
    def _get_freq_res_k_phi(delta_count, grid_size, alpha0, delta_z, beta2, k_tol, phi_tol):
        res_phi = _get_freq_res_phase_rotation(delta_count, grid_size, delta_z, beta2, phi_tol)
        res_k = _get_freq_res_dispersion_attenuation(delta_count, grid_size, alpha0, beta2, k_tol)
        res_dict = {'res_phi': res_phi, 'res_k': res_k}
        method = min(res_dict, key=res_dict.get)
        return res_dict[method], method, res_dict

    def _get_freq_res_dispersion_attenuation(delta_count, grid_size, alpha0, beta2, k_tol):
        return k_tol * abs(alpha0) / abs(beta2) / (1 + delta_count) / (4 * np.pi ** 2 * grid_size)

    def _get_freq_res_phase_rotation(delta_count, grid_size, delta_z, beta2, phi_tol):
        return phi_tol / abs(beta2) / (1 + delta_count) / delta_z / (4 * np.pi ** 2 * grid_size)

    grid_size = sim_params.nli_params.wdm_grid_size
    delta_z = sim_params.raman_params.space_resolution
    alpha0 = fiber.alpha0()
    beta2 = fiber.params.beta2
    k_tol = sim_params.nli_params.dispersion_tolerance
    phi_tol = sim_params.nli_params.phase_shift_tolerance
    f_pump_resolution, method_f_pump, res_dict_pump = \
        _get_freq_res_k_phi(0, grid_size, alpha0, delta_z, beta2, k_tol, phi_tol)
    f_cut_resolution = {}
    method_f_cut = {}
    res_dict_cut = {}
    for cut_carrier in carriers:
        delta_number = cut_carrier.channel_number - carrier.channel_number
        delta_count = abs(delta_number)
        f_res, method, res_dict = \
            _get_freq_res_k_phi(delta_count, grid_size, alpha0, delta_z, beta2, k_tol, phi_tol)
        f_cut_resolution[f'delta_{delta_number}'] = f_res
        method_f_cut[delta_number] = method
        res_dict_cut[delta_number] = res_dict
    return [f_cut_resolution, f_pump_resolution, (method_f_cut, method_f_pump), (res_dict_cut, res_dict_pump)]


def raised_cosine_comb(f, *carriers):
    """ Returns an array storing the PSD of a WDM comb of raised cosine shaped
    channels at the input frequencies defined in array f
    :param f: numpy array of frequencies in Hz
    :param carriers: namedtuple describing the WDM comb
    :return: PSD of the WDM comb evaluated over f
    """
    psd = np.zeros(np.shape(f))
    for carrier in carriers:
        f_nch = carrier.frequency
        g_ch = carrier.power.signal / carrier.baud_rate
        ts = 1 / carrier.baud_rate
        passband = (1 - carrier.roll_off) / (2 / carrier.baud_rate)
        stopband = (1 + carrier.roll_off) / (2 / carrier.baud_rate)
        ff = np.abs(f - f_nch)
        tf = ff - passband
        if carrier.roll_off == 0:
            psd = np.where(tf <= 0, g_ch, 0.) + psd
        else:
            psd = g_ch * (np.where(tf <= 0, 1., 0.) + 1 / 2 * (1 + np.cos(np.pi * ts / carrier.roll_off * tf)) *
                          np.where(tf > 0, 1., 0.) * np.where(np.abs(ff) <= stopband, 1., 0.)) + psd
    return psd


class Simulation:
    _shared_dict = {}

    def __init__(self):
        if type(self) == Simulation:
            raise NotImplementedError('Simulation cannot be instatiated')

    @classmethod
    def set_params(cls, sim_params):
        cls._shared_dict['sim_params'] = sim_params

    @classmethod
    def get_simulation(cls):
        self = cls.__new__(cls)
        return self

    @property
    def sim_params(self):
        return self._shared_dict['sim_params']


class SpontaneousRamanScattering:
    def __init__(self, frequency, z, power):
        self.frequency = frequency
        self.z = z
        self.power = power


class StimulatedRamanScattering:
    def __init__(self, frequency, z, rho, power):
        self.frequency = frequency
        self.z = z
        self.rho = rho
        self.power = power


class RamanSolver:
    def __init__(self, fiber=None):
        """ Initialize the Raman solver object.
        :param fiber: instance of elements.py/Fiber.
        :param carriers: tuple of carrier objects
        :param raman_pumps: tuple containing pumps characteristics
        """
        self._fiber = fiber
        self._carriers = None
        self._raman_pumps = None
        self._stimulated_raman_scattering = None
        self._spontaneous_raman_scattering = None

    @property
    def fiber(self):
        return self._fiber

    @property
    def carriers(self):
        return self._carriers

    @carriers.setter
    def carriers(self, carriers):
        self._carriers = carriers
        self._spontaneous_raman_scattering = None
        self._stimulated_raman_scattering = None

    @property
    def raman_pumps(self):
        return self._raman_pumps

    @raman_pumps.setter
    def raman_pumps(self, raman_pumps):
        self._raman_pumps = raman_pumps
        self._stimulated_raman_scattering = None

    @property
    def stimulated_raman_scattering(self):
        if self._stimulated_raman_scattering is None:
            if self.fiber.params.lumped_losses:
                self.calculate_stimulated_raman_scattering_losses(self.carriers, self.raman_pumps)
            else:
                self.calculate_stimulated_raman_scattering(self.carriers, self.raman_pumps)
        return self._stimulated_raman_scattering

    @property
    def spontaneous_raman_scattering(self):
        if self._spontaneous_raman_scattering is None:
            self.calculate_spontaneous_raman_scattering(self.carriers, self.raman_pumps)
        return self._spontaneous_raman_scattering

    def calculate_spontaneous_raman_scattering(self, carriers, raman_pumps):
        raman_efficiency = self.fiber.params.raman_efficiency
        temperature = self.fiber.operational['temperature']

        logger.debug('Start computing fiber Spontaneous Raman Scattering')
        power_spectrum, freq_array, prop_direct, bn_array = self._compute_power_spectrum(carriers, raman_pumps)

        alphap_fiber = self.fiber.alpha(freq_array)

        freq_diff = abs(freq_array - np.reshape(freq_array, (len(freq_array), 1)))
        interp_cr = interp1d(raman_efficiency['frequency_offset'], raman_efficiency['cr'])
        cr = interp_cr(freq_diff)

        # z propagation axis
        z_array = self.stimulated_raman_scattering.z
        ase_bc = np.zeros(freq_array.shape)

        # calculate ase power
        int_spontaneous_raman = self._int_spontaneous_raman(z_array, self._stimulated_raman_scattering.power,
                                                                   alphap_fiber, freq_array, cr, freq_diff, ase_bc,
                                                                   bn_array, temperature)

        spontaneous_raman_scattering = SpontaneousRamanScattering(freq_array, z_array, int_spontaneous_raman.x)
        logger.debug("Spontaneous Raman Scattering evaluated successfully")
        self._spontaneous_raman_scattering = spontaneous_raman_scattering


    @staticmethod
    def _compute_power_spectrum(carriers, raman_pumps=None):
        """
        Rearrangement of spectral and Raman pump information to make them compatible with Raman solver
        :param carriers: a tuple of namedtuples describing the transmitted channels
        :param raman_pumps: a namedtuple describing the Raman pumps
        :return:
        """

        # Signal power spectrum
        pow_array = np.array([])
        f_array = np.array([])
        noise_bandwidth_array = np.array([])
        for carrier in sorted(carriers, key=attrgetter('frequency')):
            f_array = np.append(f_array, carrier.frequency)
            pow_array = np.append(pow_array, carrier.power.signal)
            ref_bw = carrier.baud_rate
            noise_bandwidth_array = np.append(noise_bandwidth_array, ref_bw)

        propagation_direction = np.ones(len(f_array))

        # Raman pump power spectrum
        if raman_pumps:
            for pump in raman_pumps:
                pow_array = np.append(pow_array, pump.power)
                f_array = np.append(f_array, pump.frequency)
                direction = +1 if pump.propagation_direction.lower() == 'coprop' else -1
                propagation_direction = np.append(propagation_direction, direction)
                noise_bandwidth_array = np.append(noise_bandwidth_array, ref_bw)

        # Final sorting
        ind = np.argsort(f_array)
        f_array = f_array[ind]
        pow_array = pow_array[ind]
        propagation_direction = propagation_direction[ind]

        return pow_array, f_array, propagation_direction, noise_bandwidth_array

    def _int_spontaneous_raman(self, z_array, raman_matrix, alphap_fiber, freq_array,
                               cr_raman_matrix, freq_diff, ase_bc, bn_array, temperature):
        spontaneous_raman_scattering = OptimizeResult()

        simulation = Simulation.get_simulation()
        sim_params = simulation.sim_params

        dx = sim_params.raman_params.space_resolution
        h = ph.value('Planck constant')
        kb = ph.value('Boltzmann constant')

        power_ase = np.nan * np.ones(raman_matrix.shape)
        int_pump = cumtrapz(raman_matrix, z_array, dx=dx, axis=1, initial=0)

        for f_ind, f_ase in enumerate(freq_array):
            cr_raman = cr_raman_matrix[f_ind, :]
            vibrational_loss = f_ase / freq_array[:f_ind]
            eta = 1/(np.exp((h*freq_diff[f_ind, f_ind+1:])/(kb*temperature)) - 1)

            int_fiber_loss = -alphap_fiber[f_ind] * z_array
            int_raman_loss = np.sum((cr_raman[:f_ind] * vibrational_loss * int_pump[:f_ind, :].transpose()).transpose(),
                                    axis=0)
            int_raman_gain = np.sum((cr_raman[f_ind + 1:] * int_pump[f_ind + 1:, :].transpose()).transpose(), axis=0)

            int_gain_loss = int_fiber_loss + int_raman_gain + int_raman_loss

            new_ase = np.sum((cr_raman[f_ind+1:] * (1 + eta) * raman_matrix[f_ind+1:, :].transpose()).transpose()
                             * h * f_ase * bn_array[f_ind], axis=0)

            bc_evolution = ase_bc[f_ind] * np.exp(int_gain_loss)
            ase_evolution = np.exp(int_gain_loss) * cumtrapz(new_ase*np.exp(-int_gain_loss), z_array, dx=dx, initial=0)

            power_ase[f_ind, :] = bc_evolution + ase_evolution

        spontaneous_raman_scattering.x = 2 * power_ase
        return spontaneous_raman_scattering

    def calculate_stimulated_raman_scattering(self, carriers, raman_pumps):
        """ Returns stimulated Raman scattering solution including 
        fiber gain/loss profile.
        :return: None
        """
        # fiber parameters
        fiber_length = self.fiber.params.length
        loss_coef = self.fiber.params.lin_loss_exp
        raman_efficiency = self.fiber.params.raman_efficiency
        simulation = Simulation.get_simulation()
        sim_params = simulation.sim_params

        if not sim_params.raman_params.flag_raman:
            raman_efficiency['cr'] = np.zeros(len(raman_efficiency['cr']))
        # raman solver parameters
        z_resolution = sim_params.raman_params.space_resolution
        tolerance = sim_params.raman_params.tolerance

        logger.debug('Start computing fiber Stimulated Raman Scattering')

        power_spectrum, freq_array, prop_direct, _ = self._compute_power_spectrum(carriers, raman_pumps)

        alphap_fiber = self.fiber.alpha(freq_array)

        freq_diff = abs(freq_array - np.reshape(freq_array, (len(freq_array), 1)))
        interp_cr = interp1d(raman_efficiency['frequency_offset'], raman_efficiency['cr'])
        cr = interp_cr(freq_diff)

        # z propagation axis
        z = np.arange(0, fiber_length, z_resolution)
        z = np.concatenate((z, fiber_length), axis=None)

        ode_function = lambda z, p: self._ode_stimulated_raman(z, p, alphap_fiber, freq_array, cr, prop_direct)
        boundary_residual = lambda ya, yb: self._residuals_stimulated_raman(ya, yb, power_spectrum, prop_direct)
        initial_guess_conditions = self._initial_guess_stimulated_raman(z, power_spectrum, alphap_fiber, prop_direct)

        # ODE SOLVER
        bvp_solution = solve_bvp(ode_function, boundary_residual, z, initial_guess_conditions, tol=tolerance)

        rho = (bvp_solution.y.transpose() / power_spectrum).transpose()
        rho = np.sqrt(rho)    # From power attenuation to field attenuation
        stimulated_raman_scattering = StimulatedRamanScattering(freq_array, bvp_solution.x, rho, bvp_solution.y)

        self._stimulated_raman_scattering = stimulated_raman_scattering

    def calculate_stimulated_raman_scattering_losses(self, carriers, raman_pumps):
        """ Returns stimulated Raman scattering solution including
        fiber gain/loss profile with lumped losses aloong the fiber.
        :return: None
        """
        def defining_z_initial_phase(fiber_len, losses_obj):
            position = losses_obj['position']
            z_res = 50  # [m]
            z_arr = np.arange(0, fiber_len, z_res)
            z_arr = np.concatenate((z_arr, fiber_len), axis=None)
            z_arr = np.sort(np.unique(np.concatenate((z_arr, position), axis=None)))
            return z_arr

        def defining_z_final_phase(z_res, fiber_len, losses_obj, power_surf, z_arr):
            position = losses_obj['position']
            z_arr_new = np.arange(0, fiber_len + 1, z_res)
            z_arr_new = np.sort(np.unique(np.concatenate((z_arr_new, position), axis=None)))

            power_surface_new = np.zeros((len(power_surf[:, 0]), len(z_arr_new)))
            for f_ind, f in enumerate(power_surf):
                func_z = interp1d(z_arr, power_surf[f_ind, :])
                power_z_dummy = func_z(z_arr_new)
                power_surface_new[f_ind, :] = power_z_dummy

            return power_surface_new, z_arr_new

        def defining_loss_array(losses_obj, z_arr):
            loss = losses_obj['loss']
            position = losses_obj['position']
            loss_array = []
            for ii in range(len(z_arr)):
                if z_arr[ii] in position:
                    ind_loss = np.where(position == z_arr[ii])
                    loss_array.append(loss[np.min(ind_loss)])
                else:
                    loss_array.append(1)
            return np.array(loss_array), position

        def initial_guess_stimulated_raman_with_losses(z_arr, spectrum_pow, alphap, prop_dir, loss_arr):
            power_guess = np.empty((spectrum_pow.size, z_arr.size))
            for f_ind, pow_channel in enumerate(power_guess):

                if prop_dir[f_ind] == +1:
                    power_guess[f_ind][0] = spectrum_pow[f_ind]
                    z_indices = range(1, len(pow_channel))
                    diz = -1
                    i_loss = 0
                else:
                    power_guess[f_ind][-1] = spectrum_pow[f_ind]
                    z_indices = range(len(pow_channel) - 2, -1, -1)
                    diz = +1
                    i_loss = diz

                for z_ind in z_indices:
                    dz = abs(z_arr[z_ind] - z_arr[z_ind + diz])
                    power_guess[f_ind][z_ind] =\
                        power_guess[f_ind][z_ind + diz] * np.exp(-alphap[f_ind] * dz) / loss_arr[z_ind + i_loss]

            return power_guess

        def computing_next_order_v1(p_prev_ord, z_arr, f_array, prop_dir, alp, cr_fiber, loss_arr):
            p_next_ord = np.zeros(p_prev_ord.shape)

            dz = np.array([z_arr[i + 1] - z_arr[i] for i in range(len(z_arr) - 1)])
            dz_inv = np.array([abs(z_arr[i - 1] - z_arr[i]) for i in range(len(z_arr) - 1, 0, -1)])

            vibrational_loss = np.array([f_array[f_ind] / f_array[:f_ind] for f_ind in range(len(f_array))])
            raman_gain = np.array([np.sum(np.transpose(p_prev_ord[f_ind + 1:, :]) * cr_fiber[f_ind, f_ind + 1:], 1)
                                   for f_ind in range(len(f_array))])
            raman_loss = np.array(
                [np.sum(np.transpose(p_prev_ord[:f_ind, :]) * cr_fiber[f_ind, :f_ind] * vibrational_loss[f_ind], 1)
                 for f_ind in range(len(f_array))])
            alp_z = np.outer(-alp, np.ones(len(z_arr) - 1))

            channels_indices = np.squeeze(np.argwhere(prop_dir == +1))
            pumps_indices = np.squeeze(np.argwhere(prop_dir == -1))

            channels = np.squeeze(p_prev_ord[channels_indices, :])
            pumps = np.squeeze(p_prev_ord[pumps_indices, :])

            power_channels = channels[:, :-1]
            gamma_channels = raman_gain[channels_indices, :-1] - raman_loss[channels_indices, :-1]
            p_next_ord[channels_indices, 0] = p_prev_ord[channels_indices, 0]
            p_next_ord[channels_indices, 1:] = \
                power_channels * (1 + (alp_z[channels_indices] + gamma_channels) * dz) / loss_arr[:-1]

            power_pumps = np.flip(pumps[:, 1:], 1)
            gamma_pumps = np.flip(raman_gain[pumps_indices, 1:] - raman_loss[pumps_indices, 1:], 1)
            p_next_ord[pumps_indices, -1] = p_prev_ord[pumps_indices, -1]
            p_next_ord[pumps_indices, :-1] = np.flip(
                power_pumps * (1 + (alp_z[pumps_indices] + gamma_pumps) * dz_inv) / np.flip(loss_arr[1:]), 1)

            return p_next_ord

        def computing_next_order_v2(p_prev_ord_fast, z_arr, f_array, prop_dir, alp, cr_fiber, loss_arr):

            cop_indices = np.squeeze(np.argwhere(prop_dir == +1))
            cntp_indices = np.squeeze(np.argwhere(prop_dir == -1))
            if not cop_indices.any():
                loss_arr_inv = np.flip(loss_arr)
                dz_inv = np.abs(np.outer(np.ones(len(cntp_indices)), (np.flip(z_arr)[1:] - np.flip(z_arr)[:-1])))

                vibrational_loss = np.array([f_array[f_ind] / f_array[:f_ind] for f_ind in range(len(f_array))])
                raman_gain = np.array([np.sum(np.transpose(p_prev_ord_fast[f_ind + 1:, :])
                                              * cr_fiber[f_ind, f_ind + 1:], 1)
                                       for f_ind in range(len(f_array))])
                raman_loss = np.array([np.sum(np.transpose(p_prev_ord_fast[:f_ind, :]) * cr_fiber[f_ind, :f_ind] *
                                              vibrational_loss[f_ind], 1)
                                       for f_ind in range(len(f_array))])
                alp_z = np.outer(alp, np.ones(len(z_arr) - 1))

                gamma_cntp = -alp_z[cntp_indices] + np.flip(raman_gain[cntp_indices, 1:], 1)\
                             - np.flip(raman_loss[cntp_indices, 1:], 1)
                cntp_channels = np.squeeze(np.flip(p_prev_ord_fast[cntp_indices, :], 1))
                cntp_power = cntp_channels[:, :-1]
                cntp_dpdz_element = dz_inv * gamma_cntp * cntp_power

                updating_next_order_cntp = np.zeros([len(cntp_indices), len(z_arr)])
                updating_next_order_cntp[cntp_indices, -1] = p_prev_ord_fast[cntp_indices, -1]
                updating_next_order_cntp = np.flip(updating_next_order_cntp, 1)
                for i in range(1, len(z_arr)):
                    updating_next_order_cntp[:, i] = (updating_next_order_cntp[:, i - 1] +
                                                      cntp_dpdz_element[:, i - 1]) / loss_arr_inv[i - 1]
                    indices_zero = np.argwhere(updating_next_order_cntp[:, i] < 0)
                    updating_next_order_cntp[indices_zero, i] = 0

                p_next_ord_fast = np.flip(updating_next_order_cntp, 1)
            else:
                dz = np.outer(np.ones(len(cop_indices)), (z_arr[1:] - z_arr[:-1]))

                vibrational_loss = np.array([f_array[f_ind] / f_array[:f_ind] for f_ind in range(len(f_array))])
                raman_gain = np.array([np.sum(np.transpose(p_prev_ord_fast[f_ind + 1:, :])
                                              * cr_fiber[f_ind, f_ind + 1:], 1)
                                       for f_ind in range(len(f_array))])
                raman_loss = np.array([np.sum(np.transpose(p_prev_ord_fast[:f_ind, :]) * cr_fiber[f_ind, :f_ind] *
                                              vibrational_loss[f_ind], 1)
                                       for f_ind in range(len(f_array))])
                alp_z = np.outer(alp, np.ones(len(z_arr) - 1))

                gamma_cop = -alp_z[cop_indices] + raman_gain[cop_indices, :-1] - raman_loss[cop_indices, :-1]
                cop_channels = np.squeeze(p_prev_ord_fast[cop_indices, :])
                cop_power = cop_channels[:, :-1]
                cop_dpdz_element = dz * gamma_cop * cop_power

                updating_next_order_cop = np.zeros([len(cop_indices), len(z_arr)])
                updating_next_order_cop[cop_indices, 0] = p_prev_ord_fast[cop_indices, 0]
                for i in range(1, len(z_arr)):
                    updating_next_order_cop[:, i] = (updating_next_order_cop[:, i - 1] + cop_dpdz_element[:, i - 1]) / \
                                                    loss_arr[i - 1]
                    indices_zero = np.argwhere(updating_next_order_cop[:, i] < 0)
                    updating_next_order_cop[indices_zero, i] = 0

                p_next_ord_fast = updating_next_order_cop

            return p_next_ord_fast

        def iterative_algorithm(pow_tol, prev_order, z_arr, f_array, propagation_direction, alpha, cr_fiber,
                                loss_array, kind):
            res_err_abs = 10
            next_order = np.zeros(np.shape(prev_order))
            while res_err_abs > pow_tol:
                if kind == 'next':
                    next_order = computing_next_order_v2(prev_order, z_arr, f_array, propagation_direction, alpha,
                                                         cr_fiber, loss_array)
                else:
                    next_order = computing_next_order_v1(prev_order, z_arr, f_array, propagation_direction, alpha,
                                                         cr_fiber, loss_array)

                res_err_abs = np.max(np.abs(next_order - prev_order))
                prev_order = next_order

            return next_order

        # fiber parameters
        fiber_length = self.fiber.params.length
        loss_coef = self.fiber.params.lin_loss_exp
        raman_efficiency = self.fiber.params.raman_efficiency
        simulation = Simulation.get_simulation()
        sim_params = simulation.sim_params
        lumped_losses = self.fiber.params.lumped_losses

        if not sim_params.raman_params.flag_raman:
            raman_efficiency['cr'] = np.zeros(len(raman_efficiency['cr']))
        # raman solver parameters
        z_resolution = sim_params.raman_params.space_resolution
        tolerance = sim_params.raman_params.tolerance

        logger.debug('Start computing fiber Stimulated Raman Scattering')

        if not raman_pumps or not carriers:
            typ = 'next'
        else:
            typ = 'prev'

        power_spectrum, freq_array, prop_direct, _ = self._compute_power_spectrum(carriers, raman_pumps)

        alphap_fiber = self.fiber.alpha(freq_array)

        freq_diff = abs(freq_array - np.reshape(freq_array, (len(freq_array), 1)))
        interp_cr = interp1d(raman_efficiency['frequency_offset'], raman_efficiency['cr'])
        cr = interp_cr(freq_diff)

        # z propagation axis
        z = defining_z_initial_phase(fiber_length, lumped_losses)
        losses, z_loss = defining_loss_array(lumped_losses, z)

        initial_guess_conditions = initial_guess_stimulated_raman_with_losses(z, power_spectrum, alphap_fiber,
                                                                              prop_direct, losses)
        prev_ord = deepcopy(initial_guess_conditions)

        # ODE SOLVER
        next_ord = iterative_algorithm(tolerance, prev_ord, z, freq_array, prop_direct, alphap_fiber, cr, losses, typ)

        power_surface, z_new = defining_z_final_phase(z_resolution, fiber_length, lumped_losses, next_ord, z)

        rho = (power_surface.transpose() / power_spectrum).transpose()
        rho = np.sqrt(rho)  # From power attenuation to field attenuation
        stimulated_raman_scattering = StimulatedRamanScattering(freq_array, z_new, rho, power_surface)

        self._stimulated_raman_scattering = stimulated_raman_scattering

    def _residuals_stimulated_raman(self, ya, yb, power_spectrum, prop_direct):

        computed_boundary_value = np.zeros(ya.size)

        for index, direction in enumerate(prop_direct):
            if direction == +1:
                computed_boundary_value[index] = ya[index]
            else:
                computed_boundary_value[index] = yb[index]

        return power_spectrum - computed_boundary_value

    def _initial_guess_stimulated_raman(self, z, power_spectrum, alphap_fiber, prop_direct):
        """ Computes the initial guess knowing the boundary conditions
        :param z: patial axis [m]. numpy array
        :param power_spectrum: power in each frequency slice [W].
        Frequency axis is defined by freq_array. numpy array
        :param alphap_fiber: frequency dependent fiber attenuation of signal power [1/m].
        Frequency defined by freq_array. numpy array
        :param prop_direct: indicates the propagation direction of each power slice in power_spectrum:
        +1 for forward propagation and -1 for backward propagation. Frequency defined by freq_array. numpy array
        :return: power_guess: guess on the initial conditions [W].
        The first ndarray index identifies the frequency slice,
        the second ndarray index identifies the step in z. ndarray
        """

        power_guess = np.empty((power_spectrum.size, z.size))
        for f_index, power_slice in enumerate(power_spectrum):
            if prop_direct[f_index] == +1:
                power_guess[f_index, :] = np.exp(-alphap_fiber[f_index] * z) * power_slice
            else:
                power_guess[f_index, :] = np.exp(-alphap_fiber[f_index] * z[::-1]) * power_slice

        return power_guess

    def _ode_stimulated_raman(self, z, power_spectrum, alphap_fiber, freq_array, cr_raman_matrix, prop_direct):
        """ Aim of ode_raman is to implement the set of ordinary differential equations (ODEs)
        describing the Raman effect.
        :param z: spatial axis (unused).
        :param power_spectrum: power in each frequency slice [W].
        Frequency axis is defined by freq_array. numpy array. Size n
        :param alphap_fiber: frequency dependent fiber attenuation of signal power [1/m].
        Frequency defined by freq_array. numpy array. Size n
        :param freq_array: reference frequency axis [Hz]. numpy array. Size n
        :param cr_raman: Cr(f) Raman gain efficiency variation in frequency [1/W/m].
        Frequency defined by freq_array. numpy ndarray. Size nxn
        :param prop_direct: indicates the propagation direction of each power slice in power_spectrum:
        +1 for forward propagation and -1 for backward propagation.
        Frequency defined by freq_array. numpy array. Size n
        :return: dP/dz: the power variation in dz [W/m]. numpy array. Size n
        """

        dpdz = np.nan * np.ones(power_spectrum.shape)
        for f_ind, power in enumerate(power_spectrum):
            cr_raman = cr_raman_matrix[f_ind, :]
            vibrational_loss = freq_array[f_ind] / freq_array[:f_ind]

            for z_ind, power_sample in enumerate(power):
                raman_gain = np.sum(cr_raman[f_ind+1:] * power_spectrum[f_ind+1:, z_ind])
                raman_loss = np.sum(vibrational_loss * cr_raman[:f_ind] * power_spectrum[:f_ind, z_ind])

                dpdz_element = prop_direct[f_ind] * (-alphap_fiber[f_ind] + raman_gain - raman_loss) * power_sample
                dpdz[f_ind][z_ind] = dpdz_element

        return np.vstack(dpdz)


class NliSolver:
    """ This class implements the NLI models.
        Model and method can be specified in `sim_params.nli_params.method`.
        List of implemented methods:
        'gn_model_analytic': brute force triple integral solution
        'ggn_spectrally_separated_xpm_spm': XPM plus SPM
    """
    def __init__(self, fiber=None):
        """ Initialize the Nli solver object.
        :param fiber: instance of elements.py/Fiber.
        """
        self._fiber = fiber
        self._stimulated_raman_scattering = None

    @property
    def fiber(self):
        return self._fiber

    @property
    def stimulated_raman_scattering(self):
        return self._stimulated_raman_scattering

    @stimulated_raman_scattering.setter
    def stimulated_raman_scattering(self, stimulated_raman_scattering):
        self._stimulated_raman_scattering = stimulated_raman_scattering

    def compute_nli(self, carrier, *carriers):
        """ Compute NLI power generated by the WDM comb `*carriers` on the channel under test `carrier`
        at the end of the fiber span.
        """
        simulation = Simulation.get_simulation()
        sim_params = simulation.sim_params
        if 'gn_model_analytic' == sim_params.nli_params.nli_method_name.lower():
            carrier_nli = self._gn_analytic(carrier, *carriers)
        elif 'ggn_spectrally_separated' in sim_params.nli_params.nli_method_name.lower():
            eta_matrix = self._compute_eta_matrix(carrier, *carriers)
            carrier_nli = self._carrier_nli_from_eta_matrix(eta_matrix, carrier, *carriers)
        else:
            raise ValueError(f'Method {sim_params.nli_params.method_nli} not implemented.')

        return carrier_nli

    @staticmethod
    def _carrier_nli_from_eta_matrix(eta_matrix, carrier, *carriers):
        carrier_nli = 0
        for pump_carrier_1 in carriers:
            for pump_carrier_2 in carriers:
                carrier_nli += eta_matrix[pump_carrier_1.channel_number-1, pump_carrier_2.channel_number-1] * \
                               pump_carrier_1.power.signal * pump_carrier_2.power.signal
        carrier_nli *= carrier.power.signal

        return carrier_nli

    def _compute_eta_matrix(self, carrier_cut, *carriers):
        cut_index = carrier_cut.channel_number - 1
        simulation = Simulation.get_simulation()
        sim_params = simulation.sim_params
        # Matrix initialization
        matrix_size = max(carriers, key=lambda x: getattr(x, 'channel_number')).channel_number
        eta_matrix = np.zeros(shape=(matrix_size, matrix_size))

        # SPM
        logger.debug(f'Start computing SPM on channel #{carrier_cut.channel_number}')
        # SPM GGN
        if 'ggn' in sim_params.nli_params.nli_method_name.lower():
            partial_nli = self._generalized_spectrally_separated_spm(carrier_cut)
        # SPM GN
        elif 'gn' in sim_params.nli_params.nli_method_name.lower():
            partial_nli = self._gn_analytic(carrier_cut, *[carrier_cut])
        eta_matrix[cut_index, cut_index] = partial_nli / (carrier_cut.power.signal**3)

        # XPM
        for pump_carrier in carriers:
            pump_index = pump_carrier.channel_number - 1
            if not (cut_index == pump_index):
                logger.debug(f'Start computing XPM on channel #{carrier_cut.channel_number} '
                             f'from channel #{pump_carrier.channel_number}')
                # XPM GGN
                if 'ggn' in sim_params.nli_params.nli_method_name.lower():
                    partial_nli = self._generalized_spectrally_separated_xpm(carrier_cut, pump_carrier)
                # XPM GGN
                elif 'gn' in sim_params.nli_params.nli_method_name.lower():
                    partial_nli = self._gn_analytic(carrier_cut, *[pump_carrier])
                eta_matrix[pump_index, pump_index] = partial_nli /\
                                                     (carrier_cut.power.signal * pump_carrier.power.signal**2)
        return eta_matrix

    # Methods for computing GN-model
    def _gn_analytic(self, carrier, *carriers):
        """ Computes the nonlinear interference power on a single carrier.
        The method uses eq. 120 from arXiv:1209.0394.
        :param carrier: the signal under analysis
        :param carriers: the full WDM comb
        :return: carrier_nli: the amount of nonlinear interference in W on the carrier under analysis
        """
        beta2 = self.fiber.params.beta2
        gamma = self.fiber.params.gamma
        effective_length = self.fiber.params.effective_length
        asymptotic_length = self.fiber.params.asymptotic_length

        g_nli = 0
        for interfering_carrier in carriers:
            g_interfearing = interfering_carrier.power.signal / interfering_carrier.baud_rate
            g_signal = carrier.power.signal / carrier.baud_rate
            g_nli += g_interfearing**2 * g_signal \
                * _psi(carrier, interfering_carrier, beta2=beta2, asymptotic_length=asymptotic_length)
        g_nli *= (16.0 / 27.0) * (gamma * effective_length) ** 2 /\
                 (2 * np.pi * abs(beta2) * asymptotic_length)
        carrier_nli = carrier.baud_rate * g_nli
        return carrier_nli

    # Methods for computing the GGN-model
    def _generalized_spectrally_separated_spm(self, carrier):
        gamma = self.fiber.params.gamma
        simulation = Simulation.get_simulation()
        sim_params = simulation.sim_params
        f_cut_resolution = sim_params.nli_params.f_cut_resolution['delta_0']
        f_eval = carrier.frequency
        g_cut = (carrier.power.signal / carrier.baud_rate)

        spm_nli = carrier.baud_rate * (16.0 / 27.0) * gamma ** 2 * g_cut ** 3 * \
                  self._generalized_psi(carrier, carrier, f_eval, f_cut_resolution, f_cut_resolution)
        return spm_nli

    def _generalized_spectrally_separated_xpm(self, carrier_cut, pump_carrier):
        gamma = self.fiber.params.gamma
        simulation = Simulation.get_simulation()
        sim_params = simulation.sim_params
        delta_index = pump_carrier.channel_number - carrier_cut.channel_number
        f_cut_resolution = sim_params.nli_params.f_cut_resolution[f'delta_{delta_index}']
        f_pump_resolution = sim_params.nli_params.f_pump_resolution
        f_eval = carrier_cut.frequency
        g_pump = (pump_carrier.power.signal / pump_carrier.baud_rate)
        g_cut = (carrier_cut.power.signal / carrier_cut.baud_rate)
        frequency_offset_threshold = self._frequency_offset_threshold(pump_carrier.baud_rate)
        if abs(carrier_cut.frequency - pump_carrier.frequency) <= frequency_offset_threshold:
            xpm_nli = carrier_cut.baud_rate * (16.0 / 27.0) * gamma ** 2 * g_pump**2 * g_cut * \
                      2 * self._generalized_psi(carrier_cut, pump_carrier, f_eval, f_cut_resolution, f_pump_resolution)
        else:
            xpm_nli = carrier_cut.baud_rate * (16.0 / 27.0) * gamma ** 2 * g_pump**2 * g_cut * \
                      2 * self._fast_generalized_psi(carrier_cut, pump_carrier, f_eval, f_cut_resolution)
        return xpm_nli

    def _fast_generalized_psi(self, carrier_cut, pump_carrier, f_eval, f_cut_resolution):
        """ It computes the generalized psi function similarly to the one used in the GN model
        :return: generalized_psi
        """
        # Fiber parameters
        alpha0 = self.fiber.alpha0(f_eval)
        beta2 = self.fiber.params.beta2
        beta3 = self.fiber.params.beta3
        f_ref_beta = self.fiber.params.ref_frequency
        z = self.stimulated_raman_scattering.z
        frequency_rho = self.stimulated_raman_scattering.frequency
        rho_norm = self.stimulated_raman_scattering.rho * np.exp(np.abs(alpha0) * z / 2)
        if len(frequency_rho) == 1:
            rho_function = lambda f: rho_norm[0, :]
        else:
            rho_function = interp1d(frequency_rho, rho_norm, axis=0, fill_value='extrapolate')
        rho_norm_pump = rho_function(pump_carrier.frequency)

        f1_array = np.array([pump_carrier.frequency - (pump_carrier.baud_rate * (1 + pump_carrier.roll_off) / 2),
                             pump_carrier.frequency + (pump_carrier.baud_rate * (1 + pump_carrier.roll_off) / 2)])
        f2_array = np.arange(carrier_cut.frequency,
                             carrier_cut.frequency + (carrier_cut.baud_rate * (1 + carrier_cut.roll_off) / 2),
                             f_cut_resolution)  # Only positive f2 is used since integrand_f2 is symmetric

        integrand_f1 = np.zeros(len(f1_array))
        for f1_index, f1 in enumerate(f1_array):
            delta_beta = 4 * np.pi**2 * (f1 - f_eval) * (f2_array - f_eval) * \
                         (beta2 + np.pi * beta3 * (f1 + f2_array - 2 * f_ref_beta))
            integrand_f2 = self._generalized_rho_nli(delta_beta, rho_norm_pump, z, alpha0)
            integrand_f1[f1_index] = 2 * np.trapz(integrand_f2, f2_array)  # 2x since integrand_f2 is symmetric in f2
        generalized_psi = 0.5 * sum(integrand_f1) * pump_carrier.baud_rate
        return generalized_psi

    def _generalized_psi(self, carrier_cut, pump_carrier, f_eval, f_cut_resolution, f_pump_resolution):
        """ It computes the generalized psi function similarly to the one used in the GN model
        :return: generalized_psi
        """
        # Fiber parameters
        alpha0 = self.fiber.alpha0(f_eval)
        beta2 = self.fiber.params.beta2
        beta3 = self.fiber.params.beta3
        f_ref_beta = self.fiber.params.ref_frequency
        z = self.stimulated_raman_scattering.z
        frequency_rho = self.stimulated_raman_scattering.frequency
        rho_norm = self.stimulated_raman_scattering.rho * np.exp(np.abs(alpha0) * z / 2)
        if len(frequency_rho) == 1:
            rho_function = lambda f: rho_norm[0, :]
        else:
            rho_function = interp1d(frequency_rho, rho_norm, axis=0, fill_value='extrapolate')
        rho_norm_pump = rho_function(pump_carrier.frequency)

        f1_array = np.arange(pump_carrier.frequency - (pump_carrier.baud_rate * (1 + pump_carrier.roll_off) / 2),
                             pump_carrier.frequency + (pump_carrier.baud_rate * (1 + pump_carrier.roll_off) / 2),
                             f_pump_resolution)
        f2_array = np.arange(carrier_cut.frequency - (carrier_cut.baud_rate * (1 + carrier_cut.roll_off) / 2),
                             carrier_cut.frequency + (carrier_cut.baud_rate * (1 + carrier_cut.roll_off) / 2),
                             f_cut_resolution)
        psd1 = raised_cosine_comb(f1_array, pump_carrier) * (pump_carrier.baud_rate / pump_carrier.power.signal)

        integrand_f1 = np.zeros(len(f1_array))
        for f1_index, (f1, psd1_sample) in enumerate(zip(f1_array, psd1)):
            f3_array = f1 + f2_array - f_eval
            psd2 = raised_cosine_comb(f2_array, carrier_cut) * (carrier_cut.baud_rate / carrier_cut.power.signal)
            psd3 = raised_cosine_comb(f3_array, pump_carrier) * (pump_carrier.baud_rate / pump_carrier.power.signal)
            ggg = psd1_sample * psd2 * psd3

            delta_beta = 4 * np.pi**2 * (f1 - f_eval) * (f2_array - f_eval) * \
                         (beta2 + np.pi * beta3 * (f1 + f2_array - 2 * f_ref_beta))

            integrand_f2 = ggg * self._generalized_rho_nli(delta_beta, rho_norm_pump, z, alpha0)
            integrand_f1[f1_index] = np.trapz(integrand_f2, f2_array)
        generalized_psi = np.trapz(integrand_f1, f1_array)
        return generalized_psi

    @staticmethod
    def _generalized_rho_nli(delta_beta, rho_norm_pump, z, alpha0):
        w = 1j * delta_beta - alpha0
        generalized_rho_nli = (rho_norm_pump[-1]**2 * np.exp(w * z[-1]) - rho_norm_pump[0]**2 * np.exp(w * z[0])) / w
        for z_ind in range(0, len(z) - 1):
            derivative_rho = (rho_norm_pump[z_ind + 1]**2 - rho_norm_pump[z_ind]**2) / (z[z_ind + 1] - z[z_ind])
            generalized_rho_nli -= derivative_rho * (np.exp(w * z[z_ind + 1]) - np.exp(w * z[z_ind])) / (w**2)
        generalized_rho_nli = np.abs(generalized_rho_nli)**2
        return generalized_rho_nli

    def _frequency_offset_threshold(self, symbol_rate):
        k_ref = 5
        beta2_ref = 21.3e-27
        delta_f_ref = 50e9
        rs_ref = 32e9
        beta2 = self.fiber.params.beta2
        freq_offset_th = ((k_ref * delta_f_ref) * rs_ref * beta2_ref) / (beta2 * symbol_rate)
        return freq_offset_th

def _psi(carrier, interfering_carrier, beta2, asymptotic_length):
    """Calculates eq. 123 from `arXiv:1209.0394 <https://arxiv.org/abs/1209.0394>`__"""

    if carrier.channel_number == interfering_carrier.channel_number: # SCI, SPM
        psi = np.arcsinh(0.5 * np.pi**2 * asymptotic_length * abs(beta2) * carrier.baud_rate**2)
    else: # XCI, XPM
        delta_f = carrier.frequency - interfering_carrier.frequency
        psi = np.arcsinh(np.pi**2 * asymptotic_length * abs(beta2) *
                         carrier.baud_rate * (delta_f + 0.5 * interfering_carrier.baud_rate))
        psi -= np.arcsinh(np.pi**2 * asymptotic_length * abs(beta2) *
                          carrier.baud_rate * (delta_f - 0.5 * interfering_carrier.baud_rate))
    return psi
