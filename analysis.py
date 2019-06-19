#!/usr/bin/env python3

import os
import pathlib
import shutil
import sys
import types
import warnings

import funcy
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from pandas.core.common import SettingWithCopyWarning

from explorer import get_explorer


ex = get_explorer()
interactive = True

sns.set(style='white')

root = pathlib.Path(__file__).parent.resolve()
outputdir = root.joinpath('output')
datadir = root.joinpath('data')

warnings.simplefilter('ignore', SettingWithCopyWarning)
warnings.simplefilter('ignore', FutureWarning)


# ------------------------------------------------------------------------------
# Load data
# ------------------------------------------------------------------------------

N_TASKS = 456
TEST_ID_START = '20181024200501872083'


def _get_filters():
    return {'test_id': {'$gte': TEST_ID_START}}


def _assert_filters(df):
    assert (df['test_id'].dropna() >= TEST_ID_START).all()


def _clear_cache():
    path = datadir.joinpath('cache')
    shutil.rmtree(path)


@funcy.memoize
def _load_pipelines_df(force_download=False):
    """Get all pipelines, passing the analysis-specific test_id filter"""
    path = datadir.joinpath('cache', 'pipelines.pkl.gz')
    if force_download or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        filters = _get_filters()
        df = ex.get_pipelines(**filters)
        df.to_pickle(path, compression='gzip')
    else:
        df = pd.read_pickle(path)

    _assert_filters(df)

    return df


@funcy.memoize
def _load_baselines_df():
    df = pd.read_table(datadir.joinpath('baselines.tsv'))
    df['problem'] = df['problem'].str.replace('_problem', '')
    df = df.set_index('problem')
    _add_tscores(df, score_name='baselinescore')
    return df


def _get_best_pipeline(problem):
    filters = _get_filters()
    return ex.get_best_pipeline(problem, **filters)


# ------------------------------------------------------------------------------
# Saving results
# ------------------------------------------------------------------------------

def _savefig(fig, name, figdir=outputdir):
    figdir = pathlib.Path(figdir)
    for ext in ['.png', '.pdf', '.eps']:
        fig.savefig(figdir.joinpath(name+ext),
                    bbox_inches='tight', pad_inches=0)

# ------------------------------------------------------------------------------
# Preprocessing
# ------------------------------------------------------------------------------

_METRIC_TYPES = {
    'f1': 'zero_one_score',
    'f1Macro': 'zero_one_score',
    'accuracy': 'zero_one_score',
    'normalizedMutualInformation': 'zero_one_score',
    'meanSquaredError': 'zero_inf_cost',
    'meanAbsoluteError': 'zero_inf_cost',
    'rootMeanSquaredError': 'zero_inf_cost',
}


def _normalize(metric_type, min_value=None, max_value=None):
    def f(raw):
        if metric_type == 'zero_one_score':
            return raw
        elif metric_type == 'zero_one_cost':
            return 1 - raw
        elif metric_type == 'ranged_score':
            return (raw - min_value) / (max_value - min_value)
        elif metric_type == 'real_score':
            return 1 / (1 + np.exp(-raw))
        elif metric_type == 'real_cost':
            return 1 - (1 / (1 + np.exp(-raw)))
        elif metric_type == 'zero_inf_score':
            return 1 / (1 + np.exp(-np.log10(raw)))
        elif metric_type == 'zero_inf_cost':
            return 1 - 1 / (1 + np.exp(-np.log10(raw)))
        else:
            raise ValueError('Unknown metric type')

    return f


def _normalize_df(s, score_name='cv_score'):
    return _normalize(_METRIC_TYPES[s['metric']])(s[score_name])


def _add_tscores(df, score_name='score'):
    if 't-score' not in df:
        df['t-score'] = df.apply(_normalize_df, score_name=score_name, axis=1)


@funcy.memoize
def _get_tuning_results_df():
    df = _load_pipelines_df()

    def default_score(group):
        return group.sort_values(by='ts', ascending=True)['score'].iloc[0]

    def min_max_score(group):
        return group['score'].agg(['min', 'max'])

    default_scores = (
        df
        .groupby(['dataset', 'name'])
        .apply(default_score)
        .to_frame('default_score')
        .reset_index()
        .rename(columns = {'name': 'template'})
        [lambda _df: ~_df['template'].str.contains('trivial')]
        .groupby('dataset')
        ['default_score']
        .mean()
        .to_frame('default_score')
    )

    min_max_scores = (
        df
        .groupby('dataset')
        .apply(min_max_score)
        .rename(columns = {'min': 'min_score', 'max': 'max_score'})
    )

    sds = (
        df
        .groupby('dataset')
        ['score']
        .std()
        .to_frame('sd')
    )

    # adjust for error vs reward-style metrics (make errors negative)
    # adjustment == -1 if the metric is an Error (lower is better)
    adjustments = (
        df
        .groupby('dataset')
        ['metric']
        .first()
        .str
        .contains('Error')
        .map({True: -1, False: 1})
        .to_frame('adjustment')
    )

    data = min_max_scores.join(default_scores).join(sds).join(adjustments)

    # compute best score, adjusting for min/max
    data['best_score'] = data['max_score']
    mask = data['adjustment'] == -1
    data.loc[mask, 'best_score'] = data.loc[mask, 'min_score']

    data['delta'] = data.eval(
        expr='adjustment * (best_score - default_score) / sd')

    return data


@funcy.memoize
def _get_test_results_df():
    filters = _get_filters()
    results_df = ex.get_test_results(**filters)
    _add_tscores(results_df, score_name='cv_score')
    return results_df


@funcy.memoize
def _get_datasets_df():
    df = ex.get_datasets()
    df['dataset_id'] = df['dataset'].apply(
        ex.get_dataset_id)
    return df


# ------------------------------------------------------------------------------
# Run experiments
# ------------------------------------------------------------------------------

def make_table_2():
    df = _load_pipelines_df()
    datasets = df['dataset'].unique()

    _all_datasets = _get_datasets_df()
    _all_datasets['dataset_id'] = _all_datasets['dataset'].apply(
        ex.get_dataset_id)

    datasets_df = pd.merge(
        pd.DataFrame(datasets, columns=['dataset_id']),
        _all_datasets,
        left_on='dataset_id',
        right_on='dataset_id',
    )
    assert datasets_df.shape[0] == len(datasets)

    modality_type_count = datasets_df.groupby(
        ['data_modality', 'task_type']).size().to_frame('Tasks')
    assert modality_type_count['Tasks'].sum() == N_TASKS

    result = (
        modality_type_count
        .sort_index()
    )

    result.to_csv(outputdir.joinpath('table2.csv'))
    result.to_latex(outputdir.joinpath('table2.tex'))
    return result


def make_figure_6():
    baselines_df = _load_baselines_df()

    problems = list(baselines_df.index)
    best_pipelines = [_get_best_pipeline(problem) for problem in problems]
    mlz_pipelines_df = pd.DataFrame.from_records(
        [
            pipeline.to_dict()
            for pipeline in best_pipelines
            if pipeline is not None
        ]
    )
    mlz_pipelines_df['problem'] = mlz_pipelines_df['dataset'].str.replace(
        '_dataset_TRAIN', '')
    mlz_pipelines_df = mlz_pipelines_df.set_index('problem')
    _add_tscores(mlz_pipelines_df)

    combined_df = baselines_df.join(
        mlz_pipelines_df, lsuffix='_ll', rsuffix='_mlz')

    data = (
        combined_df[['t-score_ll', 't-score_mlz']]
        .dropna()
        .rename(columns={'t-score_ll': 'baseline',
                         't-score_mlz': 'ML Bazaar'})
        .sort_values('baseline')
        .stack()
        .to_frame('score')
        .reset_index()
        .rename(columns={'level_1': 'system'})
    )

    with sns.plotting_context('paper', font_scale=1.5):
        fig, ax = plt.subplots(figsize=(4, 8))
        sns.barplot(x='score', y='problem', hue='system', data=data)

        ax.set_xticks([0.0, 0.5, 1.0])
        ax.set_ylabel('')

        sns.despine()
        plt.tight_layout()
        ax.get_legend().remove()

        _savefig(fig, 'figure6', figdir=outputdir)
        if not interactive:
            plt.close(fig)

    fn = outputdir.joinpath('figure6.csv')
    data.to_csv(fn)

    # Compute performance vs human baseline (Section 5.3)
    result = (
        combined_df
        [['t-score_ll', 't-score_mlz']]
        .dropna()
        .apply(np.diff, axis=1)
        .agg(['mean', 'std'])
    )

    fn = outputdir.joinpath('V_B_performance_vs_baseline.csv')
    result.to_csv(fn)



def make_figure_7():
    data = _get_tuning_results_df()
    delta = data['delta'].dropna()

    with sns.plotting_context('paper', font_scale=2.0):
        fig, ax = plt.subplots(figsize=(8, 3))
        sns.distplot(delta)

        ax.set_xlabel('standard deviations')
        ax.set_ylabel('density')
        ax.set_xlim(left=0, right=5)

        sns.despine()
        plt.tight_layout()

        _savefig(fig, 'figure7', figdir=outputdir)
        if not interactive:
            plt.close(fig)

    return data


def compute_total_pipelines():
    df = _load_pipelines_df()
    n_pipelines = df.shape[0]
    result = '{} total pipelines evaluated' .format(n_pipelines)

    fn = outputdir.joinpath('total_pipelines.txt')
    with fn.open('w') as f:
        f.write(result)

    return result


def compute_pipelines_second_VI():
    test_results = _get_test_results_df()
    test_results_final = (
        test_results
        .groupby(['test_id', 'dataset'])
        .apply(lambda group: group.nlargest(1, 'elapsed'))
    )

    # filter out errored outliers with elapsed over 3h
    test_results_final = test_results_final.query('elapsed < 60*60*3')

    n_pipelines = test_results_final['iterations'].sum()
    total_seconds_elapsed = test_results_final['elapsed'].sum()
    result = n_pipelines / total_seconds_elapsed

    fn = outputdir.joinpath('VI_pipelines_second.txt')
    with fn.open('w') as f:
        f.write('{} pipelines/second'.format(result))

    return result


def compute_performance_vs_baseline_V_B():
    """Compute performance vs human baseline (Section V.B)"""
    # see make_figure_6 for implementation
    pass


def compute_tuning_improvement_sds_VI_A():
    """Compute average improvement during tuning, in sds"""
    data = _get_tuning_results_df()
    delta = data['delta'].dropna()
    result = delta.mean()

    fn = outputdir.joinpath('VI_A_tuning_improvement_sds.txt')
    with fn.open('w') as f:
        f.write(
            '{} standard deviations of improvement during tuning'
            .format(result))

    return result


def compute_tuning_improvement_pct_of_tasks_VI_A():
    """Compute pct of tasks that improve by >1sd during tuning"""
    data = _get_tuning_results_df()
    delta = data['delta'].dropna()
    result = 100 * (delta > 1.0).mean()

    fn = outputdir.joinpath('VI_A_tuning_improvement_pct_of_tasks.txt')
    with fn.open('w') as f:
        f.write(
            '{:.2f}% of tasks improve by >1 standard deviation'
            .format(result))

    return result


def compute_npipelines_xgbrf_VI_B():
    """Compute the total number of XGB/RF pipelines evaluated"""
    df = _load_pipelines_df()
    npipelines_rf = np.sum(df['pipeline'].str.contains('random_forest'))
    npipelines_xgb = np.sum(df['pipeline'].str.contains('xgb'))
    total = npipelines_rf + npipelines_xgb
    result = pd.DataFrame(
        [npipelines_rf, npipelines_xgb, total],
        index = ['RF', 'XGB', 'total'],
        columns = ['pipelines']
    )

    fn = outputdir.joinpath('VI_B_npipelines_xgbrf.csv')
    result.to_csv(fn)

    return result


def compute_xgb_wins_pct_VI_B():
    """Compute the pct of tasks for which XGB pipelines beat RF pipelines"""
    test_results_df = _get_test_results_df()

    rf_results_df = (
        test_results_df
        [lambda _df: _df['pipeline'].str.contains('random_forest').fillna(False)]
        .groupby('dataset')
        ['t-score']
        .max()
        .to_frame('RF')
    )

    xgb_results_df = (
        test_results_df
        [lambda _df: _df['pipeline'].str.contains('xgb').fillna(False)]
        .groupby('dataset')
        ['t-score']
        .max()
        .to_frame('XGB')
    )

    result = (
        rf_results_df
        .join(xgb_results_df)
        .fillna(0)
        .apply(np.argmax, axis=1)
        .value_counts()
        .to_frame('wins')
        .assign(percent = lambda _df: _df['wins'] / np.sum(_df['wins']))
    )

    # add total
    result.loc(axis=0)['total'] = result.sum()

    fn = outputdir.joinpath('VI_B_xgb_wins_pct.csv')
    result.to_csv(fn)

    return result


def compute_npipelines_maternse_VI_C():
    """Compute the total number of Matern-EI/SE-EI pipelines evaluated"""
    pipelines_df = _load_pipelines_df()
    test_results_df = _get_test_results_df()

    def find_tuner_test_ids(tuner):
        mask = test_results_df['tuner_type'].str.contains(tuner).fillna(False)
        return test_results_df.loc[mask, 'test_id']

    se_test_ids = find_tuner_test_ids('gpei')
    se_matches = pipelines_df['test_id'].isin(se_test_ids)
    se_pipelines = pipelines_df.loc[se_matches]
    n_se_pipelines = len(se_pipelines)

    matern_test_ids = find_tuner_test_ids('gpmatern52ei')
    matern_matches = pipelines_df['test_id'].isin(matern_test_ids)
    matern_pipelines = pipelines_df.loc[matern_matches]
    n_matern_pipelines = len(matern_pipelines)

    total = n_se_pipelines + n_matern_pipelines
    result = pd.DataFrame(
        [n_se_pipelines, n_matern_pipelines, total],
        index = ['SE', 'Matern52', 'total'],
        columns = ['pipelines']
    )

    fn = outputdir.joinpath('VI_C_npipelines_sematern52.csv')
    result.to_csv(fn)

    return result


def compute_matern_wins_pct_VI_C():
    """Compute the pct of tasks for which the best pipeline as tuned by GP-Matern52-EI beats the best pipeline as tuned by GP-SE-EI"""
    test_results_df = _get_test_results_df()

    gp_se_ei_results_df = (
        test_results_df
        [lambda _df: _df['tuner_type'].str.contains('gpei').fillna(False)]
        .groupby('dataset')
        ['t-score']
        .max()
        .to_frame('GP-SE-EI')
    )

    gp_matern52_ei_results_df = (
        test_results_df
        [lambda _df: _df['tuner_type'].str.contains('gpmatern52ei').fillna(False)]
        .groupby('dataset')
        ['t-score']
        .max()
        .to_frame('GP-Matern52-EI')
    )

    result = (
        gp_se_ei_results_df
        .join(gp_matern52_ei_results_df)
        .fillna(0)
        .apply(np.argmax, axis=1)
        .value_counts()
        .to_frame('wins')
        .assign(percent = lambda _df: _df['wins'] / np.sum(_df['wins']))
    )

    # add total
    result.loc(axis=0)['total'] = result.sum()

    fn = outputdir.joinpath('VI_C_matern_wins_pct.csv')
    result.to_csv(fn)

    return result


def main():
    """Call all of the results generating functions defined here"""
    this = sys.modules[__name__]
    names = set(dir(this)) - {'main'}
    for name in sorted(names):
        if name.startswith('make_') or name.startswith('compute_'):
            obj = getattr(this, name)
            if isinstance(obj, types.FunctionType):
                print('Calling {}...'.format(name))
                obj()

    print('Done.')


if __name__ == '__main__':
    interactive = False
    main()