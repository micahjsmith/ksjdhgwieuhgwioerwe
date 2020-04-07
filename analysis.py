#!/usr/bin/env python3

"""
Generate the figures and results for the paper: "The Machine Learning Bazaar:
Harnessing the ML Ecosystem for Effective System Development"
"""

import pathlib
import shutil
import sys
import types
import warnings
from contextlib import redirect_stderr, redirect_stdout
from gc import get_referents
from os import devnull
from types import FunctionType, ModuleType

import funcy as fy
import matplotlib
import matplotlib.pyplot as plt
import mit_d3m.db
import numpy as np
import pandas as pd
import seaborn as sns
from pandas.core.common import SettingWithCopyWarning
from piex.explorer import MongoPipelineExplorer, S3PipelineExplorer

warnings.simplefilter('ignore', SettingWithCopyWarning)
warnings.simplefilter('ignore', FutureWarning)

interactive = True

matplotlib.rcParams['pdf.fonttype'] = 42
matplotlib.rcParams['ps.fonttype'] = 42
sns.set(context='paper', style='white', font='serif')

ROOT = pathlib.Path(__file__).parent.resolve()
OUTPUT_DIR = ROOT.joinpath('output')
DATA_DIR = ROOT.joinpath('data')

very_dark_gray = 'dimgray'

# ------------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------------

BUCKET = 'ml-pipelines-2018'
MONGO_CONFIG_FILE = 'mongodb_config.json'


def get_explorer():
    try:
        db = mit_d3m.db.get_db(config=MONGO_CONFIG_FILE)
        return MongoPipelineExplorer(db)
    except Exception:
        return S3PipelineExplorer(BUCKET)


ex = get_explorer()


# Source: https://stackoverflow.com/a/30316760
# Custom objects know their class.
# Function objects seem to know way too much, including modules.
# Exclude modules as well.
BLACKLIST = type, ModuleType, FunctionType


def getsize(obj):
    """sum size of object & members."""
    if isinstance(obj, BLACKLIST):
        raise TypeError(
            'getsize() does not take argument of type: ' + str(type(obj)))
    seen_ids = set()
    size = 0
    objects = [obj]
    while objects:
        need_referents = []
        for obj in objects:
            if not isinstance(obj, BLACKLIST) and id(obj) not in seen_ids:
                seen_ids.add(id(obj))
                size += sys.getsizeof(obj)
                need_referents.append(obj)
        objects = get_referents(*need_referents)
    return size


@fy.decorator
def quiet(call):
    with open(devnull, 'w') as fnull:
        with redirect_stderr(fnull), redirect_stdout(fnull):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return call()


# Source: https://stackoverflow.com/a/1094933
def sizeof_fmt(num, suffix='B'):
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


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
    path = DATA_DIR.joinpath('cache')
    shutil.rmtree(path)


@fy.memoize
def _load_pipelines_df(force_download=False):
    """Get all pipelines, passing the analysis-specific test_id filter"""
    path = DATA_DIR.joinpath('cache', 'pipelines.pkl.gz')
    if force_download or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        filters = _get_filters()
        df = ex.get_pipelines(**filters)
        df.to_pickle(path, compression='gzip')
    else:
        df = pd.read_pickle(path)

    _assert_filters(df)

    return df


@fy.memoize
def _load_baselines_df():
    df = pd.read_table(DATA_DIR.joinpath('baselines.tsv'))
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

def _savefig(fig, name, figdir=OUTPUT_DIR):
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

SCORE_MAPPING = {
    'zero_one_score': lambda x, min, max: x,
    'zero_one_cost': lambda x, min, max: x,
    'ranged_score': lambda x, min, max: (x - min)/(max - min),
    'real_score': lambda x, min, max: 1 / (1+np.exp(-x)),
    'real_cost': lambda x, min, max: 1 - (1 / (1 + np.exp(-x))),
    'zero_inf_score': lambda x, min, max: 1 / (1 + np.exp(-np.log10(x))),
    'zero_inf_cost': lambda x, min, max: 1 - 1 / (1 + np.exp(-np.log10(
        x))),
}

def _make_normalizer(metric_type, min=None, max=None):
    try:
        f = SCORE_MAPPING[metric_type]
    except KeyError:
        raise ValueError('Unknown metric type: {}'.format(metric_type))
    return fy.rpartial(f, min, max)


def _normalize_df(df, score_name='cv_score'):
    normalize = _make_normalizer(_METRIC_TYPES[df['metric']])
    return normalize(df[score_name])


def _add_tscores(df, score_name='score'):
    if 't-score' not in df:
        df['t-score'] = df.apply(_normalize_df, score_name=score_name, axis=1)


@fy.memoize
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
        .rename(columns={'name': 'template'})
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
        .rename(columns={'min': 'min_score', 'max': 'max_score'})
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


@fy.memoize
def _get_test_results_df():
    filters = _get_filters()
    results_df = ex.get_test_results(**filters)
    _add_tscores(results_df, score_name='cv_score')
    return results_df


@fy.memoize
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

    result.to_csv(OUTPUT_DIR.joinpath('table2.csv'))
    result.to_latex(OUTPUT_DIR.joinpath('table2.tex'))
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

    # specifically abbreviate 'uu3_world_development_indicators'
    mask = data['problem'] == 'uu3_world_development_indicators'
    data.loc[mask, 'problem'] = 'uu3_wdi'

    with sns.plotting_context('paper'):
        fig, ax = plt.subplots(figsize=(6, 4))
        sns.barplot(x='problem', y='score', hue='system', data=data, ax=ax)

        ax.set_yticks([0.0, 0.5, 1.0])
        ax.set_xlabel('')
        plt.xticks(rotation=90)

        sns.despine(left=True, bottom=True)
        plt.tight_layout()
        ax.get_legend().remove()

        # color patches
        for (_, b2) in fy.partition(2, 2, sorted(ax.patches, key=lambda
                o: o.get_x())):
            b2.set_hatch('////')

        _savefig(fig, 'figure6', figdir=OUTPUT_DIR)
        if not interactive:
            plt.close(fig)

    fn = OUTPUT_DIR.joinpath('figure6.csv')
    data.to_csv(fn)

    # Compute performance vs human baseline (Section 5.3)
    result = (
        combined_df
        [['t-score_ll', 't-score_mlz']]
        .dropna()
        .apply(np.diff, axis=1)
        .agg(['mean', 'std'])
    )

    fn = OUTPUT_DIR.joinpath('V_B_performance_vs_baseline.csv')
    result.to_csv(fn)



def make_figure_7():
    data = _get_tuning_results_df()
    delta = data['delta'].dropna()

    with sns.plotting_context('paper', font_scale=2.0):
        fig, ax = plt.subplots(figsize=(8, 3))
        sns.distplot(delta,
                     hist_kws={'color': very_dark_gray},
                     kde_kws={'lw': 3})

        ax.set_xlabel('standard deviations')
        ax.set_ylabel('density')
        ax.set_xlim(left=0, right=5)

        sns.despine(left=True, bottom=True)
        plt.tight_layout()

        _savefig(fig, 'figure7', figdir=OUTPUT_DIR)
        if not interactive:
            plt.close(fig)

    return data


def compute_total_pipelines():
    df = _load_pipelines_df()
    n_pipelines = df.shape[0]
    result = '{} total pipelines evaluated' .format(n_pipelines)

    fn = OUTPUT_DIR.joinpath('total_pipelines.txt')
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

    fn = OUTPUT_DIR.joinpath('VI_pipelines_second.txt')
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

    fn = OUTPUT_DIR.joinpath('VI_A_tuning_improvement_sds.txt')
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

    fn = OUTPUT_DIR.joinpath('VI_A_tuning_improvement_pct_of_tasks.txt')
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
        index=['RF', 'XGB', 'total'],
        columns=['pipelines']
    )

    fn = OUTPUT_DIR.joinpath('VI_B_npipelines_xgbrf.csv')
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
        .assign(percent=lambda _df: _df['wins'] / np.sum(_df['wins']))
    )

    # add total
    result.loc(axis=0)['total'] = result.sum()

    fn = OUTPUT_DIR.joinpath('VI_B_xgb_wins_pct.csv')
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
        index=['SE', 'Matern52', 'total'],
        columns=['pipelines']
    )

    fn = OUTPUT_DIR.joinpath('VI_C_npipelines_sematern52.csv')
    result.to_csv(fn)

    return result


def compute_matern_wins_pct_VI_C():
    """Compute matern wins pct

    Compute the pct of tasks for which the best pipeline as tuned by
    GP-Matern52-EI beats the best pipeline as tuned by GP-SE-EI.
    """
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
        .assign(percent=lambda _df: _df['wins'] / np.sum(_df['wins']))
    )

    # add total
    result.loc(axis=0)['total'] = result.sum()

    fn = OUTPUT_DIR.joinpath('VI_C_matern_wins_pct.csv')
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
