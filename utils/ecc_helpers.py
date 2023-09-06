from functools import partial
import jax
from jax import lax
from jax._src.typing import Array
from jax.experimental.sparse import BCOO
import jax.numpy as jnp
import numba as nb
import numpy as np
import pickle
from scipy.spatial.distance import pdist, squareform  # type: ignore
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import eigsh
from typing import Any, Tuple, List

from solver.utils import apply_A_operator_batched
from utils.common import (SDPState,
                          scale_sdp_state,
                          unscale_sdp_state,
                          reconstruct_from_sketch,
                          apply_A_operator_mx)

from IPython import embed


def get_all_problem_data(C: BCOO) -> Tuple[BCOO, Array, Array, Array]:
    n = C.shape[0]

    # constraint: diagonal of X is all 1's
    range_n = jnp.arange(n)[:, None]
    A_indices = jnp.hstack(3*[range_n])
    A_data = jnp.ones((n,))
    b = jnp.ones((n,))
    b_ineq_mask = jnp.zeros((n,))

    # constraint: objective-relevant entries of X >= 0, written as -X <= 0
    triu_indices_mask = (C.indices[:, 0] <= C.indices[:, 1])
    constraint_indices = b.shape[0] + jnp.arange(jnp.sum(triu_indices_mask))
    constraint_triples = jnp.concatenate(
        [constraint_indices[:, None], C.indices[triu_indices_mask]], axis=1)
    constraint_triples = jnp.concatenate(
        [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
    A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
    A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), -0.5)], axis=0)
    b = jnp.concatenate([b, jnp.full((constraint_indices.shape[0],), 0.0)], axis=0)
    b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((constraint_indices.shape[0],), 1.0)], axis=0)

    return A_data, A_indices, b, b_ineq_mask


def initialize_state(C: BCOO, sketch_dim: int) -> SDPState:
    A_data, A_indices, b, b_ineq_mask = get_all_problem_data(C)
    n = C.shape[0]
    m = b.shape[0]

    SCALE_X = 1.0 / float(n)
    SCALE_C = 1.0 / jnp.linalg.norm(C.data)  # equivalent to frobenius norm
    #SCALE_A = 1.0 / jnp.sqrt(jnp.zeros((m,)).at[A_indices[:,0]].add(A_data**2))
    #A_tensor = BCOO((A_data, A_indices), shape=(m, n, n))
    #A_matrix = SCALE_A[:, None] * A_tensor.reshape(m, n**2)
    #A_matrix = coo_matrix(
    #    (A_matrix.data, (A_matrix.indices[:,0], A_matrix.indices[:,1])), shape=A_matrix.shape)
    #norm_A = jnp.sqrt(eigsh(A_matrix @ A_matrix.T, k=1, which="LM", return_eigenvectors=False)[0])
    #SCALE_A /= norm_A

    SCALE_A = jnp.ones_like(b)

    if sketch_dim == -1:
        X = jnp.zeros((n, n))
        Omega = None
        P = None
    elif sketch_dim > 0:
        X = None
        Omega = jax.random.normal(jax.random.PRNGKey(0), shape=(n, sketch_dim))
        P = jnp.zeros_like(Omega)
    else:
        raise ValueError("Invalid value for sketch_dim")

    y = jnp.zeros((m,))
    z = jnp.zeros((m,))
    tr_X = jnp.array(0.0)
    primal_obj = jnp.array(0.0)

    sdp_state = SDPState(
        C=C,
        A_indices=A_indices,
        A_data=A_data,
        b=b,
        b_ineq_mask=b_ineq_mask,
        X=X,
        P=P,
        Omega=Omega,
        y=y,
        z=z,
        tr_X=tr_X,
        primal_obj=primal_obj,
        SCALE_C=SCALE_C,
        SCALE_X=SCALE_X,
        SCALE_A=SCALE_A)

    sdp_state = scale_sdp_state(sdp_state)
    return sdp_state


def cold_start_add_constraint(
    old_sdp_state: SDPState,
    ortho_indices: List[Tuple[int, int]],
    sum_gt_one_constraints: List[List[int]],
    sketch_dim: int) -> SDPState:

    assert sketch_dim == -1
    old_sdp_state = unscale_sdp_state(old_sdp_state)

    n = old_sdp_state.C.shape[0] + 1
    C = BCOO((old_sdp_state.C.data, old_sdp_state.C.indices), shape=(n, n))

    # add the additional diagonal == 1 constraint for the new ecc
    A_indices = jnp.concatenate([old_sdp_state.A_indices,
                                 jnp.array([[old_sdp_state.b.shape[0], n-1, n-1]])], axis=0)
    A_data = jnp.concatenate([old_sdp_state.A_data, jnp.array([1.0])], axis=0)
    b = jnp.concatenate([old_sdp_state.b, jnp.array([1.0])], axis=0)
    b_ineq_mask = jnp.concatenate([old_sdp_state.b_ineq_mask, jnp.array([0.0])], axis=0)

    # add ortho indices constraints
    if len(ortho_indices) > 0:
        num_ortho_indices = len(ortho_indices)
        constraint_triples = jnp.array([[b.shape[0] + i, u, v]
                                        for i, (u, v) in enumerate(ortho_indices)])
        constraint_triples = jnp.concatenate(
            [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
        A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
        A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), 1.0)], axis=0)
        b = jnp.concatenate([b, jnp.full((num_ortho_indices,), 0.0)], axis=0)
        b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((num_ortho_indices,), 1.0)], axis=0)

    ## singleton expansion: if a satisfying hyperplane can only be satisfied by one
    ##   point, then that means the representation for the ecc and the point must
    ##   be exactly the same. To make optimization easier we add extra hyperplanes,
    ##   replacing the ecc index with the singleton index
    #supp_constraints = []
    #for hyperplane in [h for h in sum_gt_one_constraints if len(h) == 1]:
    #    _, point_idx = hyperplane[0]
    #    for other_hyperplane in sum_gt_one_constraints:
    #        if other_hyperplane != hyperplane:
    #            supp_constraints.append([(point_idx, v) for _, v in other_hyperplane])
    #sum_gt_one_constraints += supp_constraints

    # add sum greater than one (feature satisfying hyperplanes) constraints
    num_hyperplanes = len(sum_gt_one_constraints)
    constraint_triples = jnp.array([(b.shape[0] + i, u, v)
                                    for i, pairs in enumerate(sum_gt_one_constraints)
                                    for u, v in pairs])
    constraint_triples = jnp.concatenate(
        [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
    A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
    A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), -0.5)], axis=0)
    b = jnp.concatenate([b, jnp.full((num_hyperplanes,), -1.0)], axis=0)
    b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((num_hyperplanes,), 1.0)], axis=0)

    m = b.shape[0]

    if sketch_dim == -1:
        X = jnp.zeros((n, n))
        Omega = None
        P = None
    elif sketch_dim > 0:
        X = None
        Omega = jax.random.normal(jax.random.PRNGKey(0), shape=(n, sketch_dim))
        P = jnp.zeros_like(Omega)
    else:
        raise ValueError("Invalid value for sketch_dim")

    y = jnp.zeros((m,))
    z = jnp.zeros((m,))
    tr_X = jnp.array(0.0)
    primal_obj = jnp.array(0.0)

    SCALE_X = 1.0 / float(n)
    SCALE_C = 1.0 / jnp.linalg.norm(C.data)  # equivalent to frobenius norm
    #SCALE_A = 1.0 / jnp.sqrt(jnp.zeros((m,)).at[A_indices[:,0]].add(A_data**2))
    #A_tensor = BCOO((A_data, A_indices), shape=(m, n, n))
    #A_matrix = SCALE_A[:, None] * A_tensor.reshape(m, n**2)
    #A_matrix = coo_matrix(
    #    (A_matrix.data, (A_matrix.indices[:,0], A_matrix.indices[:,1])), shape=A_matrix.shape)
    #norm_A = jnp.sqrt(eigsh(A_matrix @ A_matrix.T, k=1, which="LM", return_eigenvectors=False)[0])
    #SCALE_A /= norm_A

    SCALE_A = jnp.ones_like(b)

    sdp_state = SDPState(
        C=C,
        A_indices=A_indices,
        A_data=A_data,
        b=b,
        b_ineq_mask=b_ineq_mask,
        X=X,
        P=P,
        Omega=Omega,
        y=y,
        z=z,
        tr_X=tr_X,
        primal_obj=primal_obj,
        SCALE_C=SCALE_C,
        SCALE_X=SCALE_X,
        SCALE_A=SCALE_A)

    sdp_state = scale_sdp_state(sdp_state)
    return sdp_state


def warm_start_add_constraint(
    old_sdp_state: SDPState,
    ortho_indices: List[Tuple[int, int]],
    sum_gt_one_constraints: List[List[int]],
    prev_pred_clusters: Array,
    sketch_dim: int) -> SDPState:

    assert sketch_dim == -1
    old_sdp_state = unscale_sdp_state(old_sdp_state)

    old_n = old_sdp_state.C.shape[0]
    n = old_n + 1
    C = BCOO((old_sdp_state.C.data, old_sdp_state.C.indices), shape=(n, n))

    # add the additional diagonal == 1 constraint for the new ecc
    A_indices = jnp.concatenate([old_sdp_state.A_indices,
                                 jnp.array([[old_sdp_state.b.shape[0], n-1, n-1]])], axis=0)
    A_data = jnp.concatenate([old_sdp_state.A_data, jnp.array([1.0])], axis=0)
    b = jnp.concatenate([old_sdp_state.b, jnp.array([1.0])], axis=0)
    b_ineq_mask = jnp.concatenate([old_sdp_state.b_ineq_mask, jnp.array([0.0])], axis=0)

    # add ortho indices constraints
    if len(ortho_indices) > 0:
        num_ortho_indices = len(ortho_indices)
        constraint_triples = jnp.array([[b.shape[0] + i, u, v]
                                        for i, (u, v) in enumerate(ortho_indices)])
        constraint_triples = jnp.concatenate(
            [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
        A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
        A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), 1.0)], axis=0)
        b = jnp.concatenate([b, jnp.full((num_ortho_indices,), 0.0)], axis=0)
        b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((num_ortho_indices,), 1.0)], axis=0)

    ## singleton expansion: if a satisfying hyperplane can only be satisfied by one
    ##   point, then that means the representation for the ecc and the point must
    ##   be exactly the same. To make optimization easier we add extra hyperplanes,
    ##   replacing the ecc index with the singleton index
    #supp_constraints = []
    #for hyperplane in [h for h in sum_gt_one_constraints if len(h) == 1]:
    #    _, point_idx = hyperplane[0]
    #    for other_hyperplane in sum_gt_one_constraints:
    #        if other_hyperplane != hyperplane:
    #            supp_constraints.append([(point_idx, v) for _, v in other_hyperplane])
    #sum_gt_one_constraints += supp_constraints

    # add sum greater than one (feature satisfying hyperplanes) constraints
    num_hyperplanes = len(sum_gt_one_constraints)
    constraint_triples = jnp.array([(b.shape[0] + i, u, v)
                                    for i, pairs in enumerate(sum_gt_one_constraints)
                                    for u, v in pairs])
    constraint_triples = jnp.concatenate(
        [constraint_triples, constraint_triples[:, [0, 2, 1]]], axis=0)
    A_indices = jnp.concatenate([A_indices, constraint_triples], axis=0)
    A_data = jnp.concatenate([A_data, jnp.full((constraint_triples.shape[0],), -0.5)], axis=0)
    b = jnp.concatenate([b, jnp.full((num_hyperplanes,), -1.0)], axis=0)
    b_ineq_mask = jnp.concatenate([b_ineq_mask, jnp.full((num_hyperplanes,), 1.0)], axis=0)

    m = b.shape[0]

    columns_to_drop = [v for l in sum_gt_one_constraints for pairs in l for v in pairs if len(l) <= 2]
    equality_columns = [v for l in sum_gt_one_constraints for pairs in l for v in pairs if len(l) == 1]

    ecc_points_and_counts = [(pairs[1], len(l)) for l in sum_gt_one_constraints for pairs in l if len(l) < 3]
    ecc_points_and_counts = jnp.array(list(set(ecc_points_and_counts)))
    ecc_points = ecc_points_and_counts[:, 0]
    ecc_counts = ecc_points_and_counts[:, 1]

    columns_to_drop = jnp.array(list(set(columns_to_drop)))
    equality_columns = jnp.array(list(set(equality_columns)))
    equality_columns = equality_columns[equality_columns < old_n]

    neg_points = jnp.array([v for v, _ in ortho_indices])

    num_pred_clusters = max(jnp.unique(prev_pred_clusters).shape[0], 2)

    nbr_ecc_points = np.where(np.isin(prev_pred_clusters, prev_pred_clusters[ecc_points]))[0]

    if len(ortho_indices) > 0:
        nbr_ecc_points = nbr_ecc_points[~np.isin(nbr_ecc_points, neg_points)]

    X = old_sdp_state.X
    Omega = old_sdp_state.Omega
    P = old_sdp_state.P
    if old_sdp_state.X is not None:
        # compute rank-`num_pred_clusters` approximation of X
        eigvals, eigvecs = jnp.linalg.eigh(old_sdp_state.X)
        point_embeds = (eigvecs[:,-num_pred_clusters:] * jnp.sqrt(eigvals[None, -num_pred_clusters:]))
        point_embeds = point_embeds / jnp.linalg.norm(point_embeds, axis=1)[:, None]

        avg_embed = jnp.sum(point_embeds[ecc_points] / ecc_counts[:, None], axis=0)
        avg_embed = avg_embed / jnp.linalg.norm(avg_embed)

        #point_embeds = point_embeds.at[nbr_ecc_points].set(
        #    point_embeds[nbr_ecc_points] + avg_embed[None, :])
        #point_embeds = point_embeds.at[ecc_points].set(
        #    point_embeds[ecc_points] + avg_embed[None, :])

        point_embeds = point_embeds.at[ecc_points].set(avg_embed[None, :])
        #point_embeds = point_embeds.at[nbr_ecc_points].set(avg_embed[None, :])
        point_embeds = jnp.concatenate([point_embeds, avg_embed[None, :]], axis=0)

        #if len(ortho_indices) > 0:
        #    neg_point_embeds = point_embeds[neg_points]
        #    neg_point_embeds = neg_point_embeds / np.linalg.norm(neg_point_embeds, axis=1)[:, None]
        #    neg_point_projs = np.dot(neg_point_embeds, avg_embed)
        #    neg_point_projs = neg_point_projs / (np.linalg.norm(neg_point_embeds, axis=1) ** 2)
        #    neg_point_projs = neg_point_projs[:, None] * avg_embed
        #    neg_point_embeds = neg_point_embeds - neg_point_projs
        #    point_embeds = point_embeds.at[neg_points, :].set(neg_point_embeds)

        point_embeds = point_embeds / jnp.linalg.norm(point_embeds, axis=1)[:, None]

        #point_embeds = point_embeds.at[neg_points].set(jnp.zeros_like(point_embeds[neg_points]))

        X = point_embeds @ point_embeds.T

        #X = jnp.zeros_like(X)

        #X = BCOO.fromdense(X_trunc)
        #X = BCOO((X.data, X.indices), shape=(n, n)).todense()
        #X = BCOO.fromdense(X)
        ##drop_mask = jnp.isin(X.indices, columns_to_drop)
        #drop_mask = jnp.isin(X.indices, nbr_ecc_points)
        #drop_mask = (drop_mask[:, 0] | drop_mask[:, 1])
        #X = BCOO((X.data[~drop_mask], X.indices[~drop_mask]), shape=(n, n)).todense()

        z = apply_A_operator_mx(n, m, A_data, A_indices, X) 
    if old_sdp_state.P is not None:
        assert False

    tr_X = jnp.trace(X)
    primal_obj = jnp.trace(C @ X)

    SCALE_X = 1.0 / float(n)
    SCALE_C = 1.0 / jnp.linalg.norm(C.data)  # equivalent to frobenius norm
    #SCALE_A = 1.0 / jnp.sqrt(jnp.zeros((m,)).at[A_indices[:,0]].add(A_data**2))
    #A_tensor = BCOO((A_data, A_indices), shape=(m, n, n))
    #A_matrix = SCALE_A[:, None] * A_tensor.reshape(m, n**2)
    #A_matrix = coo_matrix(
    #    (A_matrix.data, (A_matrix.indices[:,0], A_matrix.indices[:,1])), shape=A_matrix.shape)
    #norm_A = jnp.sqrt(eigsh(A_matrix @ A_matrix.T, k=1, which="LM", return_eigenvectors=False)[0])
    #SCALE_A /= norm_A

    SCALE_A = jnp.ones_like(b)

    y = jnp.zeros((m,)).at[jnp.arange(old_sdp_state.b.shape[0])].set(
        old_sdp_state.y / old_sdp_state.SCALE_A)
    y = y * (SCALE_X / old_sdp_state.SCALE_X) * SCALE_A

    y = y + (0.05 / (2 * SCALE_X) * jnp.clip(b - z, a_max=0.0))

    #y = y.at[neg_points].set(jnp.zeros_like(y[neg_points]))

    #y = y.at[ecc_points].set(jnp.zeros_like(y[ecc_points]))
    #y = jnp.full((m,), -.0 * SCALE_X).at[jnp.arange(old_sdp_state.b.shape[0])].set(
    #    old_sdp_state.y / old_sdp_state.SCALE_A)

    ## set the dual variable
    #old_diag_indices = old_sdp_state.A_indices[
    #    old_sdp_state.A_indices[:,1] ==old_sdp_state. A_indices[:,2]][:,0]
    #old_equality_mask = jnp.isin(old_diag_indices, jnp.where(1 - old_sdp_state.b_ineq_mask)[0])
    #old_diag_indices = old_diag_indices[old_equality_mask]

    ##mean_equality_dual = jnp.mean(old_sdp_state.y[old_diag_indices])
    ##mean_inequality_dual = jnp.mean(
    ##    old_sdp_state.y[~jnp.isin(jnp.arange(old_sdp_state.b.shape[0]), old_diag_indices)])
    #mean_inequality_dual = jnp.mean(old_sdp_state.y / old_sdp_state.SCALE_A)

    #diag_indices = A_indices[A_indices[:,1] == A_indices[:,2]][:,0]
    #equality_mask = jnp.isin(diag_indices, jnp.where(1 - b_ineq_mask)[0])
    #diag_indices = diag_indices[equality_mask]

    ## TODO: see if we want to increase dual variable for pos ecc point diag equalities 
    #mean_inequality_dual = 0.0
    #y = jnp.full((m,), 5.0 * mean_inequality_dual).at[diag_indices].set(5.0 * mean_inequality_dual)
    #y = y.at[jnp.arange(old_sdp_state.b.shape[0])].set(old_sdp_state.y / old_sdp_state.SCALE_A)

    ##print("y mean equality value: ", mean_equality_dual)
    #print("y mean inequality value: ", mean_inequality_dual)

    # TODO: change this to be rho instead of 0.05

    # drop relevant entries in y
    #reset_constraint_mask = (jnp.isin(A_indices[:, 1], columns_to_drop)
    #                         & jnp.isin(A_indices[:, 2], columns_to_drop))
    #y = y.at[jnp.unique(A_indices[reset_constraint_mask, 0])].set(0.0)

    sdp_state = SDPState(
        C=C,
        A_indices=A_indices,
        A_data=A_data,
        b=b,
        b_ineq_mask=b_ineq_mask,
        X=X,
        P=P,
        Omega=Omega,
        y=y,
        z=z,
        tr_X=tr_X,
        primal_obj=primal_obj,
        SCALE_C=SCALE_C,
        SCALE_X=SCALE_X,
        SCALE_A=SCALE_A)

    sdp_state = scale_sdp_state(sdp_state)
    return sdp_state