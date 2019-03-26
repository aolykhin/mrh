'''
pyscf-CASSCF SOLVER for DMET

To use this solver, one need to modify the dmet module to recognize the pyscf_amoscf.
Specifically:
line 33: assert (( method == 'ED' ) or ( method == 'CC' ) or ( method == 'MP2' ) or ( method == 'CASSCF' ))
line 257-261:
elif ( self.method == 'CASSCF' ):
    import pyscf_amoscf
    assert( Nelec_in_imp % 2 == 0 )
    guess_1RDM = self.ints.dmet_init_guess_rhf( loc2dmet, Norb_in_imp, Nelec_in_imp//2, numImpOrbs, chempot_frag )
    IMP_energy, IMP_1RDM = pyscf_amoscf.solve( 0.0, dmetOEI, dmetFOCK, dmetTEI, Norb_in_imp, Nelec_in_imp, numImpOrbs, guess_1RDM, chempot_frag )

History: 

- the solver is tested under FCI limit. The energy agrees with the FCI energy by chemps2 solver.
However, the energy is explosive when the active space decreasing. VERY negative! => SOLVED

- Need to improve the efficiency => SOLVED

author: Hung Pham (email: phamx494@umn.edu)
'''

import numpy as np
from scipy import linalg
from mrh.my_dmet import localintegrals
import os, time
import sys, copy
#import qcdmet_paths
from pyscf import gto, scf, ao2mo, mcscf, fci, lib
from pyscf.symm.addons import label_orb_symm
from pyscf.mcscf.addons import spin_square
from pyscf.fci.addons import transform_ci_for_orbital_rotation
from pyscf.tools import molden
#np.set_printoptions(threshold=np.nan)
from mrh.util.la import matrix_eigen_control_options, matrix_svd_control_options
from mrh.util.basis import represent_operator_in_basis, project_operator_into_subspace, orthonormalize_a_basis, get_complete_basis, get_complementary_states
from mrh.util.basis import is_basis_orthonormal, get_overlapping_states, is_basis_orthonormal_and_complete, compute_nelec_in_subspace, get_subspace_symmetry_blocks
from mrh.util.basis import measure_subspace_blockbreaking, measure_basis_nonorthonormality, cleanup_subspace_symmetry
from mrh.util.rdm import get_2CDM_from_2RDM, get_2RDM_from_2CDM
from mrh.util.io import prettyprint_ndarray as prettyprint
from mrh.util.tensors import symmetrize_tensor
from mrh.my_pyscf import mcscf as my_mcscf
from mrh.my_pyscf.scf import hf_as
from mrh.my_pyscf.fci import csf_solver
from mrh.my_pyscf.mcscf import fix_ci_response_csf
from functools import reduce, partial

#def solve( CONST, OEI, FOCK, TEI, frag.norbs_imp, frag.nelec_imp, frag.norbs_frag, impCAS, frag.active_orb_list, guess_1RDM, energytype='CASCI', chempot_frag=0.0, printoutput=True ):
def solve (frag, guess_1RDM, chempot_imp):

    # Augment OEI with the chemical potential
    OEI = frag.impham_OEI_C - chempot_imp

    # Do I need to get the full RHF solution?
    guess_orbs_av = len (frag.imp_cache) == 2 or frag.norbs_as > 0 

    # Get the RHF solution
    mol = gto.Mole()
    abs_2MS = int (round (2 * abs (frag.target_MS)))
    abs_2S = int (round (2 * abs (frag.target_S)))
    mol.spin = abs_2MS
    mol.verbose = 0 if frag.mol_output is None else lib.logger.DEBUG
    mol.output = frag.mol_output
    mol.atom.append(('H', (0, 0, 0)))
    mol.nelectron = frag.nelec_imp
    if frag.enforce_symmetry:
        mol.groupname  = frag.symmetry
        mol.symm_orb   = get_subspace_symmetry_blocks (frag.loc2imp, frag.loc2symm)
        mol.irrep_name = frag.ir_names
        mol.irrep_id   = frag.ir_ids
    mol.build ()
    if frag.enforce_symmetry: mol.symmetry = True
    #mol.incore_anyway = True
    mf = scf.RHF(mol)
    mf.get_hcore = lambda *args: OEI
    mf.get_ovlp = lambda *args: np.eye(frag.norbs_imp)
    mf.energy_nuc = lambda *args: frag.impham_CONST
    if frag.impham_CDERI is not None:
        mf = mf.density_fit ()
        mf.with_df._cderi = frag.impham_CDERI
    else:
        mf._eri = ao2mo.restore(8, frag.impham_TEI, frag.norbs_imp)
    mf.__dict__.update (frag.mf_attr)
    if mol.spin != 0:
        # Dress get_fock for inter-fragment exchange
        def my_fock (my_mf, h1e=None, s1e=None, vhf=None, dm=None, cycle=-1, diis=None,
            diis_start_cycle=None, level_shift_factor=None, damp_factor=None):
            if vhf is None: vhf = my_mf.get_veff(my_mf.mol, dm)
            vhf[0] += frag.impham_OEI_S
            vhf[1] -= frag.impham_OEI_S
            return mf.__class__.get_fock (my_mf, h1e=h1e, s1e=s1e, vhf=vhf, dm=dm, cycle=cycle,
                diis=diis, diis_start_cycle=diis_start_cycle, level_shift_factor=level_shift_factor,
                damp_factor=damp_factor)
        mf.get_fock = partial (my_fock, mf)
    if guess_orbs_av: mf.max_cycle = 2
    mf.scf (guess_1RDM)
    if (not mf.converged) and (not guess_orbs_av):
        print ("CASSCF RHF-step not converged on fixed-point iteration; initiating newton solver")
        mf = mf.newton ()
        mf.kernel ()

    # Instability check and repeat
    if not guess_orbs_av:
        for i in range (frag.num_mf_stab_checks):
            mf.mo_coeff = mf.stability ()[0]
            guess_1RDM = mf.make_rdm1 ()
            mf = scf.RHF(mol)
            mf.get_hcore = lambda *args: OEI
            mf.get_ovlp = lambda *args: np.eye(frag.norbs_imp)
            mf._eri = ao2mo.restore(8, frag.impham_TEI, frag.norbs_imp)
            mf.scf (guess_1RDM)
            if not mf.converged:
                mf = mf.newton ()
                mf.kernel ()

    # Correct for same-spin correlation
    E_RHF = mf.e_tot
    idx_somo = np.isclose (mf.mo_occ, 1)
    if np.count_nonzero (idx_somo):
        print ("Found {} singly-occupied molecular orbitals in CASSCF-RHF step".format (np.count_nonzero (idx_somo)))
        imp2somo = mf.mo_coeff[:,idx_somo]
        SSK = np.trace (represent_operator_in_basis (frag.impham_OEI_S, imp2somo))
        print ("Adding interfragment same-spin exchange energy ({}) to CASSCF-RHF energy ({})".format (SSK, E_RHF))
        E_RHF += SSK
    print ("CASSCF RHF-step energy: {}".format (E_RHF))

    # Get the CASSCF solution
    CASe = frag.active_space[0]
    CASorb = frag.active_space[1] 
    checkCAS =  (CASe <= frag.nelec_imp) and (CASorb <= frag.norbs_imp)
    if (checkCAS == False):
        CASe = frag.nelec_imp
        CASorb = frag.norbs_imp
    if (abs_2MS > abs_2S):
        CASe = ((CASe + abs_2S) // 2, (CASe - abs_2S) // 2)
    else:
        CASe = ((CASe + abs_2MS) // 2, (CASe - abs_2MS) // 2)
    if frag.impham_CDERI is not None:
        mc = mcscf.DFCASSCF(mf, CASorb, CASe)
    else:
        mc = mcscf.CASSCF(mf, CASorb, CASe)
    smult = abs_2S + 1 if frag.target_S is not None else (frag.nelec_imp % 2) + 1
    mc.fcisolver = csf_solver (mf.mol, smult, symm=frag.enforce_symmetry)
    if frag.enforce_symmetry: mc.fcisolver.wfnsym = frag.wfnsym
    mc.max_cycle_macro = 50 if frag.imp_maxiter is None else frag.imp_maxiter
    mc.ah_start_tol = 1e-10
    mc.ah_conv_tol = 1e-10
    mc.conv_tol = 1e-9
    mc.__dict__.update (frag.corr_attr)
    mc = fix_my_CASSCF_for_nonsinglet_env (mc, frag.impham_OEI_S)
    norbs_amo = mc.ncas
    norbs_cmo = mc.ncore
    norbs_imo = frag.norbs_imp - norbs_amo
    nelec_amo = sum (mc.nelecas)
    norbs_occ = norbs_amo + norbs_cmo
    #mc.natorb = True

    # Guess orbitals
    ci0 = None
    dm_imp = frag.get_oneRDM_imp ()
    fock_imp = mf.get_fock (dm=dm_imp)
    if len (frag.imp_cache) == 2:
        imp2mo, ci0 = frag.imp_cache
        print ("Taking molecular orbitals and ci vector from cache")
    elif frag.norbs_as > 0:
        nelec_imp_guess = int (round (np.trace (frag.oneRDMas_loc)))
        norbs_cmo_guess = (frag.nelec_imp - nelec_imp_guess) // 2
        print ("Projecting stored amos (frag.loc2amo; spanning {} electrons) onto the impurity basis and filling the remainder with default guess".format (nelec_imp_guess))
        imp2mo, my_occ = project_amo_manually (frag.loc2imp, frag.loc2amo, fock_imp, norbs_cmo_guess, dm=frag.oneRDMas_loc)
    elif frag.loc2amo_guess is not None:
        print ("Projecting stored amos (frag.loc2amo_guess) onto the impurity basis (no amo dm available)")
        imp2mo, my_occ = project_amo_manually (frag.loc2imp, frag.loc2amo_guess, fock_imp, norbs_cmo, dm=None)
        frag.loc2amo_guess = None
    else:
        dm_imp = np.asarray (mf.make_rdm1 ())
        while dm_imp.ndim > 2:
            dm_imp = dm_imp.sum (0)
        imp2mo = mf.mo_coeff
        fock_imp = mf.get_fock (dm=dm_imp)
        fock_mo = represent_operator_in_basis (fock_imp, imp2mo)
        _, evecs = matrix_eigen_control_options (fock_mo, sort_vecs=1)
        imp2mo = imp2mo @ evecs
        my_occ = ((dm_imp @ imp2mo) * imp2mo).sum (0)
        print ("No stored amos; using mean-field canonical MOs as initial guess")
    # Guess orbital processing
    if callable (frag.cas_guess_callback):
        mo = reduce (np.dot, (frag.ints.ao2loc, frag.loc2imp, imp2mo))
        mo = frag.cas_guess_callback (frag.ints.mol, mc, mo)
        imp2mo = reduce (np.dot, (frag.imp2loc, frag.ints.ao2loc.conjugate ().T, frag.ints.ao_ovlp, mo))
        frag.cas_guess_callback = None

    # Guess CI vector
    if len (frag.imp_cache) != 2 and frag.ci_as is not None:
        loc2amo_guess = np.dot (frag.loc2imp, imp2mo[:,norbs_cmo:norbs_occ])
        gOc = np.dot (loc2amo_guess.conjugate ().T, frag.ci_as_orb)
        umat_g, svals, umat_c = matrix_svd_control_options (gOc, sort_vecs=-1, only_nonzero_vals=True)
        if (svals.size == norbs_amo):
            print ("Loading ci guess despite shifted impurity orbitals; singular value sum: {}".format (np.sum (svals)))
            imp2mo[:,norbs_cmo:norbs_occ] = np.dot (imp2mo[:,norbs_cmo:norbs_occ], umat_g)
            ci0 = transform_ci_for_orbital_rotation (frag.ci_as, CASorb, CASe, umat_c)
        else:
            print ("Discarding stored ci guess because orbitals are too different (missing {} nonzero svals)".format (norbs_amo-svals.size))

    # Symmetry align if possible
    imp2mo[:,:norbs_cmo] = frag.align_imporbs_symm (imp2mo[:,:norbs_cmo], sorting_metric=fock_imp, sort_vecs=1, orbital_type='guess inactive', mol=mol)[0]
    imp2mo[:,norbs_cmo:norbs_occ], umat = frag.align_imporbs_symm (imp2mo[:,norbs_cmo:norbs_occ],
                                                                          sorting_metric=fock_imp, sort_vecs=1, orbital_type='guess active', mol=mol)
    imp2mo[:,norbs_occ:] = frag.align_imporbs_symm (imp2mo[:,norbs_occ:], sorting_metric=fock_imp, sort_vecs=1, orbital_type='guess external', mol=mol)[0]
    if frag.enforce_symmetry:
        imp2mo = cleanup_subspace_symmetry (imp2mo, mol.symm_orb)
        err_symm = measure_subspace_blockbreaking (imp2mo, mol.symm_orb)
        err_orth = measure_basis_nonorthonormality (imp2mo)
        print ("Initial symmetry error after cleanup = {}".format (err_symm))
        print ("Initial orthonormality error after cleanup = {}".format (err_orth))
    if ci0 is not None: ci0 = transform_ci_for_orbital_rotation (ci0, CASorb, CASe, umat)
        

    # Guess orbital printing
    if frag.mfmo_printed == False:
        ao2mfmo = reduce (np.dot, [frag.ints.ao2loc, frag.loc2imp, imp2mo])
        print ("Writing {} {} orbital molden".format (frag.frag_name, 'CAS guess'))
        molden.from_mo (frag.ints.mol, frag.filehead + frag.frag_name + '_mfmorb.molden', ao2mfmo, occ=my_occ)
        frag.mfmo_printed = True
    elif len (frag.active_orb_list) > 0: # This is done AFTER everything else so that the _mfmorb.molden always has consistent ordering
        print('Applying caslst: {}'.format (frag.active_orb_list))
        imp2mo = mc.sort_mo(frag.active_orb_list, mo_coeff=imp2mo)
        frag.active_orb_list = []
    if len (frag.frozen_orb_list) > 0:
        mc.frozen = copy.copy (frag.frozen_orb_list)
        print ("Applying frozen-orbital list (this macroiteration only): {}".format (frag.frozen_orb_list))
        frag.frozen_orb_list = []

    if frag.enforce_symmetry: imp2mo = lib.tag_array (imp2mo, orbsym=label_orb_symm (mol, mol.irrep_id, mol.symm_orb, imp2mo, s=mf.get_ovlp (), check=False))

    t_start = time.time()
    E_CASSCF = mc.kernel(imp2mo, ci0)[0]
    if not mc.converged:
        print ("Hello")
        mc = mc.newton ()
        E_CASSCF = mc.kernel(mc.mo_coeff, mc.ci)[0]
    if not mc.converged:
        print ('Assuming ci vector is poisoned; discarding...')
        imp2mo = mc.mo_coeff.copy ()
        mc = mcscf.CASSCF(mf, CASorb, CASe)
        smult = abs_2S + 1 if frag.target_S is not None else (frag.nelec_imp % 2) + 1
        mc.fcisolver = csf_solver (mf.mol, smult)
        E_CASSCF = mc.kernel(imp2mo)[0]
        if not mc.converged:
            mc = mc.newton ()
            E_CASSCF = mc.kernel(mc.mo_coeff, mc.ci)[0]
    assert (mc.converged)

    '''
    mc.conv_tol = 1e-12
    mc.ah_start_tol = 1e-10
    mc.ah_conv_tol = 1e-12
    E_CASSCF = mc.kernel(mc.mo_coeff, mc.ci)[0]
    if not mc.converged:
        mc = mc.newton ()
        E_CASSCF = mc.kernel(mc.mo_coeff, mc.ci)[0]
    #assert (mc.converged)
    '''
    
    # Get twoRDM + oneRDM. cs: MC-SCF core, as: MC-SCF active space
    # I'm going to need to keep some representation of the active-space orbitals

    # Symmetry align if possible
    oneRDM_amo, twoRDM_amo = mc.fcisolver.make_rdm12 (mc.ci, mc.ncas, mc.nelecas)
    fock_imp = mc.get_fock ()
    mc.mo_coeff[:,:norbs_cmo] = frag.align_imporbs_symm (mc.mo_coeff[:,:norbs_cmo], sorting_metric=fock_imp, sort_vecs=1, orbital_type='optimized inactive', mol=mol)[0]
    mc.mo_coeff[:,norbs_cmo:norbs_occ], umat = frag.align_imporbs_symm (mc.mo_coeff[:,norbs_cmo:norbs_occ],
        sorting_metric=oneRDM_amo, sort_vecs=-1, orbital_type='optimized active', mol=mol)
    mc.mo_coeff[:,norbs_occ:] = frag.align_imporbs_symm (mc.mo_coeff[:,norbs_occ:], sorting_metric=fock_imp, sort_vecs=1, orbital_type='optimized external', mol=mol)[0]
    if frag.enforce_symmetry:
        amo2imp = mc.mo_coeff[:,norbs_cmo:norbs_occ].conjugate ().T
        mc.mo_coeff = cleanup_subspace_symmetry (mc.mo_coeff, mol.symm_orb)
        umat = umat @ (amo2imp @ mc.mo_coeff[:,norbs_cmo:norbs_occ])
        err_symm = measure_subspace_blockbreaking (mc.mo_coeff, mol.symm_orb)
        err_orth = measure_basis_nonorthonormality (mc.mo_coeff)
        print ("Final symmetry error after cleanup = {}".format (err_symm))
        print ("Final orthonormality error after cleanup = {}".format (err_orth))
    mc.ci = transform_ci_for_orbital_rotation (mc.ci, CASorb, CASe, umat)

    # Cache stuff
    imp2mo = mc.mo_coeff #mc.cas_natorb()[0]
    loc2mo = np.dot (frag.loc2imp, imp2mo)
    imp2amo = imp2mo[:,norbs_cmo:norbs_occ]
    loc2amo = loc2mo[:,norbs_cmo:norbs_occ]
    frag.imp_cache = [mc.mo_coeff, mc.ci]
    frag.ci_as = mc.ci
    frag.ci_as_orb = loc2amo.copy ()
    t_end = time.time()

    # oneRDM
    oneRDM_imp = mc.make_rdm1 ()

    # twoCDM
    oneRDM_amo, twoRDM_amo = mc.fcisolver.make_rdm12 (mc.ci, mc.ncas, mc.nelecas)
    oneRDMs_amo = np.stack (mc.fcisolver.make_rdm1s (mc.ci, mc.ncas, mc.nelecas), axis=0)
    oneRSM_amo = oneRDMs_amo[0] - oneRDMs_amo[1] if frag.target_MS >= 0 else oneRDMs_amo[1] - oneRDMs_amo[0]
    oneRSM_imp = represent_operator_in_basis (oneRSM_amo, imp2amo.conjugate ().T)
    print ("Norm of spin density: {}".format (linalg.norm (oneRSM_amo)))
    # Note that I do _not_ do the *real* cumulant decomposition; I do one assuming oneRDMs_amo_alpha = oneRDMs_amo_beta
    # This is fine as long as I keep it consistent, since it is only in the orbital gradients for this impurity that
    # the spin density matters. But it has to stay consistent!
    twoCDM_amo = get_2CDM_from_2RDM (twoRDM_amo, oneRDM_amo)
    twoCDM_imp = represent_operator_in_basis (twoCDM_amo, imp2amo.conjugate ().T)
    print('Impurity CASSCF energy (incl chempot): {}; spin multiplicity: {}; time to solve: {}'.format (E_CASSCF, spin_square (mc)[1], t_end - t_start))

    # General impurity data
    frag.oneRDM_loc = symmetrize_tensor (frag.oneRDMfroz_loc + represent_operator_in_basis (oneRDM_imp, frag.imp2loc))
    frag.twoCDM_imp = None # Experiment: this tensor is huge. Do I actually need to keep it? In principle, of course not.
    frag.E_imp      = E_CASSCF + np.einsum ('ab,ab->', chempot_imp, oneRDM_imp)

    # Active-space RDM data
    frag.oneRDMas_loc  = symmetrize_tensor (represent_operator_in_basis (oneRDM_amo, loc2amo.conjugate ().T))
    frag.oneRSMas_loc  = symmetrize_tensor (represent_operator_in_basis (oneRSM_amo, loc2amo.conjugate ().T))
    frag.twoCDMimp_amo = twoCDM_amo
    frag.loc2mo = loc2mo
    frag.loc2amo = loc2amo
    frag.E2_cum  = np.tensordot (ao2mo.restore (1, mc.get_h2eff (), mc.ncas), twoCDM_amo, axes=4) / 2
    frag.E2_cum += (mf.get_k (dm=oneRSM_imp) * oneRSM_imp).sum () / 4
    # The second line compensates for my incorrect cumulant decomposition. Anything to avoid changing the checkpoint files...

    return None

def project_amo_manually (loc2imp, loc2gamo, fock_mf, norbs_cmo, dm=None):
    norbs_amo = loc2gamo.shape[1]
    amo2imp = np.dot (loc2gamo.conjugate ().T, loc2imp)
    ovlp = np.dot (amo2imp, amo2imp.conjugate ().T)
    '''
    print ("Do impurity orbitals span guess amos?")
    print (prettyprint (ovlp, fmt='{:5.2f}'))
    if dm is not None:
        print ("Density matrix?")
        print (prettyprint (represent_operator_in_basis (dm, loc2gamo), fmt='{:5.2f}'))
    '''
    proj = np.dot (amo2imp.conjugate ().T, amo2imp)
    evals, evecs = matrix_eigen_control_options (proj, sort_vecs=-1, only_nonzero_vals=False)
    imp2amo = np.copy (evecs[:,:norbs_amo])
    imp2imo = np.copy (evecs[:,norbs_amo:])
    fock_imo = represent_operator_in_basis (fock_mf, imp2imo)
    _, evecs = matrix_eigen_control_options (fock_imo, sort_vecs=1, only_nonzero_vals=False)
    imp2imo = np.dot (imp2imo, evecs)
    imp2cmo = imp2imo[:,:norbs_cmo]
    imp2vmo = imp2imo[:,norbs_cmo:]
    # Sort amo in order to apply stored ci vector
    imp2gamo = np.dot (loc2imp.conjugate ().T, loc2gamo)
    amoOgamo = np.dot (imp2amo.conjugate ().T, imp2gamo)
    #print ("Overlap matrix between guess-active and active:")
    #print (prettyprint (amoOgamo, fmt='{:5.2f}'))
    Pgamo1_amo = np.einsum ('ik,jk->ijk', amoOgamo, amoOgamo.conjugate ())
    imp2ramo = np.zeros_like (imp2amo)
    ramo_evals = np.zeros (imp2ramo.shape[1], dtype=imp2ramo.dtype)
    while (Pgamo1_amo.shape[0] > 0):
        max_eval = 0
        argmax_eval = -1
        argmax_evecs = None
        for idx in range (Pgamo1_amo.shape[2]):
            evals, evecs = matrix_eigen_control_options (Pgamo1_amo[:,:,idx], sort_vecs=-1, only_nonzero_vals=False)
            if evals[0] > max_eval:
                max_eval = evals[0]
                max_evecs = evecs
                argmax_eval = idx
        #print ("With {} amos to go, assigned highest eigenvalue ({}) to {}".format (Pgamo1_amo.shape[0], max_eval, argmax_eval))
        ramo_evals[argmax_eval] = max_eval
        imp2ramo[:,argmax_eval] = np.einsum ('ij,j->i', imp2amo, max_evecs[:,0])
        imp2amo = np.dot (imp2amo, max_evecs[:,1:])
        amoOgamo = np.dot (imp2amo.conjugate ().T, imp2gamo)
        Pgamo1_amo = np.einsum ('ik,jk->ijk', amoOgamo, amoOgamo.conjugate ())
    imp2amo = imp2ramo
    print ("Fidelity of projection of guess active orbitals onto impurity space:\n{}".format (ramo_evals))
    amoOgamo = np.dot (imp2amo.conjugate ().T, imp2gamo)
    idx_signflip = np.diag (amoOgamo) < 0
    imp2amo[:,idx_signflip] *= -1
    amoOgamo = np.dot (imp2amo.conjugate ().T, imp2gamo)
    ''' 
    print ("Overlap matrix between guess-active and active:")
    print (prettyprint (amoOgamo, fmt='{:5.2f}'))
    O = np.dot (imp2amo.conjugate ().T, imp2amo) - np.eye (imp2amo.shape[1]) 
    print ("Overlap error between active and active: {}".format (linalg.norm (O)))
    O = np.dot (imp2amo.conjugate ().T, imp2cmo)    
    print ("Overlap error between active and occupied: {}".format (linalg.norm (O)))
    O = np.dot (imp2amo.conjugate ().T, imp2vmo)    
    print ("Overlap error between active and virtual: {}".format (linalg.norm (O)))
    '''
    my_occ = np.zeros (loc2imp.shape[1], dtype=np.float64)
    my_occ[:norbs_cmo] = 2
    my_occ[norbs_cmo:][:imp2amo.shape[1]] = 1
    if dm is not None:
        loc2amo = np.dot (loc2imp, imp2amo)
        evals, evecs = matrix_eigen_control_options (represent_operator_in_basis (dm, loc2amo), sort_vecs=-1, only_nonzero_vals=False)
        imp2amo = np.dot (imp2amo, evecs)
        print ("Guess density matrix eigenvalues for guess amo: {}".format (evals))
        my_occ[norbs_cmo:][:imp2amo.shape[1]] = evals
    imp2mo = np.concatenate ([imp2cmo, imp2amo, imp2vmo], axis=1)
    return imp2mo, my_occ

def make_guess_molden (frag, filename, imp2mo, norbs_cmo, norbs_amo):
    norbs_tot = imp2mo.shape[1]
    mo_occ = np.zeros (norbs_tot)
    norbs_occ = norbs_cmo + norbs_amo
    mo_occ[:norbs_cmo] = 2
    mo_occ[norbs_cmo:norbs_occ] = 1
    mo = reduce (np.dot, (frag.ints.ao2loc, frag.loc2imp, imp2mo))
    molden.from_mo (frag.ints.mol, filename, mo, occ=mo_occ)
    return

def fix_my_CASSCF_for_nonsinglet_env (mc, h1e_s):
    ''' Strategy: cache the spin-breaking potential in the full basis in the mc object and in the
    active subspace in the mc.fcisolver object. Update the latter at every call to mc.casci.
    For the inner-cycle subspace ci response, hack it into mc.update_casdm and
    mc.solve_approx_ci using the envs kwarg. Wrap mc.fcisolver.kernel with a CONDITIONAL inspection
    of h1e (if and only if the h1e passed as an argument is one-component, add the second component).
    Finally, wrap gen_g_hop for the orbital rotation by just adding the various derivatives
    of h1e_s - should be straightforward. '''

    mc = fix_ci_response_csf (mc)
    if h1e_s is None: return mc

    class fixed_FCI (mc.fcisolver.__class__):

        def __init__(self, my_fci, my_h1e_s):
            self.__dict__.update (my_fci.__dict__)
            self.h1e_s_cache = my_h1e_s
            self._keys = self.__dict__.keys ()
            # The only function of the last line is to prevent lib.check_sanity from annoying me

        def kernel (self, h1e, eri, norb, nelec, ci0=None, **kwargs):
            if np.asarray (h1e).ndim == 2:
                h1e = [h1e, self.h1e_s_cache]
            return super().kernel (h1e, eri, norb, nelec, ci0=ci0, **kwargs)

        def make_rdm12 (self, ci, ncas, nelecas):
            ''' I need to smuggle in the spin-density matrix for the sake of gen_g_hop down there. '''
            dm1, dm2 = super().make_rdm12 (ci, ncas, nelecas)
            dm1a, dm1b = self.make_rdm1s (ci, ncas, nelecas)
            dm1 = lib.tag_array (dm1, sdm=dm1a-dm1b)
            return dm1, dm2
            
    class fixed_CASSCF (mc.__class__):

        def __init__(self, my_mc, my_h1e_s):
            self.__dict__.update (my_mc.__dict__)
            amo = my_mc.mo_coeff[:,my_mc.ncore:my_mc.ncore+my_mc.ncas]
            amoH = amo.conjugate ().T
            self.fcisolver = fixed_FCI (my_mc.fcisolver, amoH @ my_h1e_s @ amo)
            self.h1e_s = my_h1e_s
            self._keys = self.__dict__.keys ()
            # The only function of the last line is to prevent lib.check_sanity from annoying me

        def casci (self, mo_coeff, ci0=None, eris=None, verbose=None, envs=None):
            ''' path of least resistance: just cache h1e_s in the fcisolver '''
            amo = mo_coeff[:,self.ncore:self.ncore+self.ncas]
            amoH = amo.conjugate ().T
            self.fcisolver.h1e_s_cache = amoH @ self.h1e_s @ amo
            return super().casci (mo_coeff, ci0=ci0, eris=eris, verbose=verbose, envs=envs)
            
        def update_casdm (self, mo, u, fcivec, e_cas, eris, envs={}):
            ''' ~look-of-disapproval~ I need to hack h1e in with the 'envs' kwarg because Qiming or whoever
            didn't properly use a single function to generate h1e.
            Every call to self.fcisolver methods downstream of this will goes through solve_approx_ci
            and will be using an explicitly two-component h1, so don't mess with
            self.fcisolver.h1e_s_cache here.'''
            mou = mo @ u[:,self.ncore:self.ncore+self.ncas]
            mouH = mou.conjugate ().T
            # Beware, envs came from the "local ()" function upstream, which will cause problems if I edit it.  Make a copy!
            my_envs = envs.copy ()
            my_envs['h1e_s'] = mouH @ self.h1e_s @ mou
            return super().update_casdm (mo, u, fcivec, e_cas, eris, envs=my_envs)

        def solve_approx_ci (self, h1, h2, ci0, ecore, e_cas, envs):
            ''' ~look-of-disapproval~ I shouldn't have to edit this at all; see update_casdm above. '''
            h1 = np.stack ([h1, envs['h1e_s']], axis=0)
            return super().solve_approx_ci (h1, h2, ci0, ecore, e_cas, envs)

        def gen_g_hop (self, mo, u, casdm1, casdm2, eris):
            ''' I do not get why u is an argument of this function.  It doesn't appear to be
            referenced within it. 

            It'll be a trick and a half to test the Hessian. There's no straightforward
            signature of having a wrong answer; the convergence will just get really bad.'''
            g_orb, gorb_update, h_op, h_diag = super ().gen_g_hop (mo, u, casdm1, casdm2, eris)
            g_orb = self.unpack_uniq_var (g_orb) # Makes a lower-triangular matrix
            g_orb = g_orb - g_orb.T
            h_diag = self.unpack_uniq_var (h_diag)
            h_diag = h_diag + h_diag.T

            ncore = self.ncore
            ncas = self.ncas
            nocc = ncore + ncas
            nelecas = self.nelecas
            h1e_s_mo = np.zeros_like (self.h1e_s)
            sdm_mo = np.zeros_like (self.h1e_s)
            h1e_s_mo = mo.conjugate ().T @ self.h1e_s @ mo
            sdm_mo[ncore:nocc, ncore:nocc] = casdm1.sdm
            sdm_mo = sdm_mo + sdm_mo.T

            gen_k = h1e_s_mo @ sdm_mo
            g_orb += gen_k - gen_k.T
            g_orb = self.pack_uniq_var (g_orb)
            def my_gorb_update (u, fcivec):
                g_orb_u  = self.unpack_uniq_var (gorb_update (u, fcivec)) # makes a lower-triangular matrix
                g_orb_u -= g_orb_u.T
                dma, dmb = self.fcisolver.make_rdm1s (fcivec, ncas, nelecas)
                sdm      = dma - dmb
                uH       = u.conjugate ().T
                h1e_s_u  = uH @ h1e_s_mo @ u
                sdm_u    = np.zeros_like (h1e_s_u)
                sdm_u[ncore:nocc, ncore:nocc]  = sdm
                sdm_u[ncore:nocc, ncore:nocc] += sdm.T
                gen_k_u  = h1e_s_u @ sdm_u
                g_orb_u += gen_k_u - gen_k_u.T
                return self.pack_uniq_var (g_orb_u)

            sdm_t     = sdm_mo + sdm_mo.T
            h_diag_s  = np.outer (np.diag (h1e_s_mo), np.diag (sdm_t))
            h_diag_s -= h1e_s_mo * sdm_mo
            h_diag_s -= np.diag (gen_k)[:,None]
            h_diag += h_diag_s + h_diag_s.T
            h_diag  = self.pack_uniq_var (h_diag)

            def my_h_op (x):
                hx  = h_op (x)
                hx  = self.unpack_uniq_var (hx)
                x1  = self.unpack_uniq_var (x)
                x1 -= x1.T

                hx += h1e_s_mo @ x1 @ sdm_mo
                hx += sdm_mo @ x1 @ h1e_s_mo

                hx -= gen_k @ x1
                hx -= x1 @ gen_k.T

                return self.pack_uniq_var (hx - hx.T)

            return g_orb, my_gorb_update, my_h_op, h_diag

    return fixed_CASSCF (mc, h1e_s)







