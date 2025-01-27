#################################
# - Detrend and Estimate TTVs - #
#################################

# This script detrends raw Kepler PDCSAP Flux to produce two data products
# (1) flattened lightcurves with low frequency (t >> T14) flux variation removed
# (2) estimates of individual transit times (i.e. TTVs) including a regularized model
#
# The script is initialized with an input CSV file specifying pre-identified planets (e.g. Kepler's DR25 catalog)

import os
import sys
import glob
import shutil
import warnings
from datetime import datetime
from timeit import default_timer as timer

print("")
print("+"*shutil.get_terminal_size().columns)
print("ALDERAAN Detrending and TTV Estimation")
print("Initialized {0}".format(datetime.now().strftime("%d-%b-%Y at %H:%M:%S")))
print("+"*shutil.get_terminal_size().columns)
print("")

# start program timer
global_start_time = timer()


# parse inputs
import argparse
import matplotlib as mpl

parser = argparse.ArgumentParser(description="Inputs for ALDERAAN transit fiting pipeline")

parser.add_argument("--mission", default=None, type=str, required=True, 
                    help="Mission name; can be 'Kepler' or 'Simulated'")
parser.add_argument("--target", default=None, type=str, required=True,
                    help="Target name; format should be K00000 or S00000")
parser.add_argument("--root_dir", default=None, type=str, required=True,
                    help="Root directory for system")
parser.add_argument("--project_dir", default=None, type=str, required=True,
                    help="Project directory for accessing lightcurve data and saving outputs; i.e. <root_dir>/<project_dir>")
parser.add_argument("--catalog", default=None, type=str, required=True,
                    help="CSV file containing input planet parameters; should be placed in <project_dir>/Catalogs/")
parser.add_argument("--interactive", default=False, type=bool, required=False,
                    help="'True' to enable interactive plotting; by default matplotlib backend will be set to 'Agg'")

args = parser.parse_args()
MISSION      = args.mission
TARGET       = args.target
ROOT_DIR     = args.root_dir
PROJECT_DIR  = ROOT_DIR + args.project_dir
CATALOG      = args.catalog  

# set plotting backend
if args.interactive == False:
    mpl.use('agg')

    
# set environment variables
sys.path.append(PROJECT_DIR)


# echo pipeline info
print("")
print("   MISSION : {0}".format(MISSION))
print("   TARGET  : {0}".format(TARGET))
print("")
print("   Project directory : {0}".format(PROJECT_DIR))
print("   Input catalog     : {0}".format(CATALOG))
print("")


# build directory structure
if MISSION == 'Kepler': DOWNLOAD_DIR = PROJECT_DIR + 'MAST_downloads/'
if MISSION == 'Simulated': DOWNLOAD_DIR = PROJECT_DIR + 'Simulations/'

# directories in which to place pipeline outputs
FIGURE_DIR    = PROJECT_DIR + 'Figures/' + TARGET + '/'
TRACE_DIR     = PROJECT_DIR + 'Traces/' + TARGET + '/'
QUICK_TTV_DIR = PROJECT_DIR + 'QuickTTVs/' + TARGET + '/'
DLC_DIR       = PROJECT_DIR + 'Detrended_lightcurves/' + TARGET + '/'
NOISE_DIR     = PROJECT_DIR + 'Noise_models/' + TARGET + '/'

# check if all the output directories exist and if not, create them
if os.path.exists(FIGURE_DIR) == False:
    os.mkdir(FIGURE_DIR)

if os.path.exists(TRACE_DIR) == False:
    os.mkdir(TRACE_DIR)
    
if os.path.exists(QUICK_TTV_DIR) == False:
    os.mkdir(QUICK_TTV_DIR)
    
if os.path.exists(DLC_DIR) == False:
    os.mkdir(DLC_DIR)
    
if os.path.exists(NOISE_DIR) == False:
    os.mkdir(NOISE_DIR)


# import packages
import astropy
from   astropy.io import fits
from   astropy.timeseries import LombScargle
import glob
import lightkurve as lk
import matplotlib.pyplot as plt
import numpy as np
import numpy.polynomial.polynomial as poly
import pandas as pd
from   scipy import ndimage
from   scipy import stats

import pymc3 as pm
import pymc3_ext as pmx
import exoplanet as exo
import aesara_theano_fallback.tensor as T
from   aesara_theano_fallback import aesara as theano
from   celerite2.theano import GaussianProcess
from   celerite2.theano import terms as GPterms

from   alderaan.constants import *
from   alderaan.utils import bin_data, boxcar_smooth, get_transit_depth, LS_estimator
import alderaan.detrend as detrend
import alderaan.io as io
import alderaan.omc as omc
from   alderaan.LiteCurve import LiteCurve
from   alderaan.Planet import Planet

# flush buffer to avoid mixed outputs from progressbar
sys.stdout.flush()

# turn off FutureWarnings
warnings.filterwarnings('ignore', category=FutureWarning)

# check for interactive matplotlib backends
if np.any(np.array(['agg', 'png', 'svg', 'pdf', 'ps']) == mpl.get_backend()):
    iplot = False
else:
    iplot = True
    
# echo theano cache directory
print("theano cache: {0}\n".format(theano.config.compiledir))


################
# - DATA I/O - #
################

print("\nLoading data...\n")

# !!!WARNING!!! Kepler reference epochs are not always consistent between catalogs. If using DR25, you will need to correct from BJD to BJKD with an offset of 2454833.0 days - the exoplanet archive has already converted epochs to BJKD

# read in planet and star properties from csv file
target_dict = pd.read_csv(PROJECT_DIR + 'Catalogs/' + CATALOG)

# set KOI_ID global variable
if MISSION == 'Kepler':
    KOI_ID = TARGET
elif MISSION == 'Simulated':
    KOI_ID = 'K' + TARGET[1:]
else:
    raise ValueError("MISSION must be 'Kepler' or 'Simulated'")

# pull relevant quantities and establish GLOBAL variables
use = np.array(target_dict['koi_id']) == KOI_ID

KIC = np.array(target_dict['kic_id'], dtype='int')[use]
NPL = np.array(target_dict['npl'], dtype='int')[use]

U1 = np.array(target_dict['limbdark_1'], dtype='float')[use]
U2 = np.array(target_dict['limbdark_2'], dtype='float')[use]

PERIODS = np.array(target_dict['period'], dtype='float')[use]
EPOCHS  = np.array(target_dict['epoch'],  dtype='float')[use]
DEPTHS  = np.array(target_dict['depth'], dtype='float')[use]*1e-6          # [ppm] --> []
DURS    = np.array(target_dict['duration'], dtype='float')[use]/24         # [hrs] --> [days]
IMPACTS = np.array(target_dict['impact'], dtype='float')[use]

# do some consistency checks
if all(k == KIC[0] for k in KIC): KIC = KIC[0]
else: raise ValueError("There are inconsistencies with KIC in the csv input file")

if all(n == NPL[0] for n in NPL): NPL = NPL[0]
else: raise ValueError("There are inconsistencies with NPL in the csv input file")

if all(u == U1[0] for u in U1): U1 = U1[0]
else: raise ValueError("There are inconsistencies with U1 in the csv input file")

if all(u == U2[0] for u in U2): U2 = U2[0]
else: raise ValueError("There are inconsistencies with U2 in the csv input file")

if np.any(np.isnan(PERIODS)): raise ValueError("NaN values found in input catalog")
if np.any(np.isnan(EPOCHS)):  raise ValueError("NaN values found in input catalog")
if np.any(np.isnan(DEPTHS)):  raise ValueError("NaN values found in input catalog")
if np.any(np.isnan(DURS)):    raise ValueError("NaN values found in input catalog")
if np.any(np.isnan(IMPACTS)): raise ValueError("NaN values found in input catalog")


# Read in pre-downloaded lightcurve data
# Kepler data can be retrieved by running the script "download_from_MAST.py"
# Simulated data can be produced by running the script "simulate_lightcurve.py"

# short cadence (load all available)
try:
    if MISSION == 'Kepler':
        sc_path  = glob.glob(DOWNLOAD_DIR + 'mastDownload/Kepler/kplr' + '{0:09d}'.format(KIC) + '*_sc*/')[0]
        sc_files = glob.glob(sc_path + '*')

        sc_rawdata_list = []
        for i, scf in enumerate(sc_files):
            sc_rawdata_list.append(lk.read(sc_files[i]))

        sc_raw_collection = lk.LightCurveCollection(sc_rawdata_list)
        sc_data = io.cleanup_lkfc(sc_raw_collection, KIC)


    elif MISSION == 'Simulated':
        sc_path = DOWNLOAD_DIR + 'Lightcurves/Kepler/simkplr' + '{0:09d}'.format(KIC) + '_sc/'
        sc_files = glob.glob(sc_path + '*')

        sc_rawdata_list = []
        for i, scf in enumerate(sc_files):
            sc_rawdata_list.append(io.load_sim_fits(scf))

        quarters = []
        for i, scrd in enumerate(sc_rawdata_list):
            quarters.append(scrd.quarter)

        order = np.argsort(quarters)
        sc_rawdata_list = [sc_rawdata_list[j] for j in order]

        sc_raw_collection = lk.LightCurveCollection(sc_rawdata_list)
        sc_data = io.cleanup_lkfc(sc_raw_collection)


except:
    sc_data = lk.LightCurveCollection([])

    
sc_quarters = []
for i, scd in enumerate(sc_data):
    sc_quarters.append(scd.quarter)


# long cadence (only load quarters for which short cadence data do not exist)
try:
    if MISSION == 'Kepler':
        lc_path  = glob.glob(DOWNLOAD_DIR + 'mastDownload/Kepler/kplr' + '{0:09d}'.format(KIC) + '*_lc*/')[0]
        lc_files = glob.glob(lc_path + '*')

        lc_rawdata_list = []
        for i, lcf in enumerate(lc_files):
            lkread = lk.read(lc_files[i])

            if ~np.isin(lkread.quarter, sc_quarters):
                lc_rawdata_list.append(lkread)

        lc_raw_collection = lk.LightCurveCollection(lc_rawdata_list)
        lc_data = io.cleanup_lkfc(lc_raw_collection, KIC)


    elif MISSION == 'Simulated':
        lc_path = DOWNLOAD_DIR + 'Lightcurves/Kepler/simkplr' + '{0:09d}'.format(KIC) + '_lc/'
        lc_files = glob.glob(lc_path + '*')

        lc_rawdata_list = []
        for i, lcf in enumerate(lc_files):
            lc_rawdata_list.append(io.load_sim_fits(lcf))


        quarters = []
        for i, lcrd in enumerate(lc_rawdata_list):
            quarters.append(lcrd.quarter)

        order = np.argsort(quarters)
        lc_rawdata_list = [lc_rawdata_list[j] for j in order]

        lc_raw_collection = lk.LightCurveCollection(lc_rawdata_list)
        lc_data = io.cleanup_lkfc(lc_raw_collection, KIC)


except:
    lc_data = lk.LightCurveCollection([])

    
lc_quarters = []
for i, lcd in enumerate(lc_data):
    lc_quarters.append(lcd.quarter)


# convert lk.Lightcurve to custom LiteCurve objects
sc_lite = []
lc_lite = []

for i, scd in enumerate(sc_data):
    sc_lite.append(io.LightKurve_to_LiteCurve(scd))
    
for i, lcd in enumerate(lc_data):
    lc_lite.append(io.LightKurve_to_LiteCurve(lcd))
    
# revert variable names
sc_data = sc_lite
lc_data = lc_lite


#####################
# - PRELIMINARIES - #
#####################

# Establish time baseline

print("Establishing observation baseline")

time_min = []
time_max = []

for i, scd in enumerate(sc_data):
    time_min.append(scd.time.min())
    time_max.append(scd.time.max())

for i, lcd in enumerate(lc_data):
    time_min.append(lcd.time.min())
    time_max.append(lcd.time.max())

TIME_START = np.min(time_min)
TIME_END   = np.max(time_max)

if TIME_START < 0:
    raise ValueError("START TIME [BKJD] is negative...this will cause problems")


# put epochs in range (TIME_START, TIME_START + PERIOD)
for npl in range(NPL):
    if EPOCHS[npl] < TIME_START:
        adj = 1 + (TIME_START - EPOCHS[npl])//PERIODS[npl]
        EPOCHS[npl] += adj*PERIODS[npl]        
        
    if EPOCHS[npl] > (TIME_START + PERIODS[npl]):
        adj = (EPOCHS[npl] - TIME_START)//PERIODS[npl]
        EPOCHS[npl] -= adj*PERIODS[npl]


# Initialize Planet objects

print("Initializing {0} Planet objects".format(NPL))

planets = []
for npl in range(NPL):
    p = Planet()
    
    # put in some basic transit parameters
    p.epoch    = EPOCHS[npl]
    p.period   = PERIODS[npl]
    p.depth    = DEPTHS[npl]
    p.duration = DURS[npl]
    p.impact   = IMPACTS[npl]
    
    if p.impact > 1 - np.sqrt(p.depth):
        p.impact = (1 - np.sqrt(p.depth))**2
        
    # estimate transit times from linear ephemeris
    p.tts = np.arange(p.epoch, TIME_END, p.period)

    # make transit indexes
    p.index = np.array(np.round((p.tts-p.epoch)/p.period),dtype='int')
    
    # add to list
    planets.append(p)


# put planets in order by period
order = np.argsort(PERIODS)

sorted_planets = []
for npl in range(NPL):
    sorted_planets.append(planets[order[npl]])

planets = np.copy(sorted_planets)


##########################
# - TRANSIT TIME SETUP - #
##########################

print("\nBuilding initial TTV model...\n")


# Build starting TTV model
# "ephemeris" always refers to a *linear* ephemeris
# "transit_times" include non-linear TTVs (when they exist)

# use Holczer+ 2016 TTVs where they exist
HOLCZER_FILE = PROJECT_DIR + 'Catalogs/holczer_2016_kepler_ttvs.txt'


if MISSION == 'Kepler':
    holczer_data = np.loadtxt(HOLCZER_FILE, usecols=[0,1,2,3])

    holczer_inds = []
    holczer_tts  = []
    holczer_pers = []

    for npl in range(NPL):
        koi = int(TARGET[1:]) + 0.01*(1+npl)
        use = np.isclose(holczer_data[:,0], koi, rtol=1e-10, atol=1e-10)
        
        # Holczer uses BJD -24548900; BJKD = BJD - 2454833
        if np.sum(use) > 0:
            holczer_inds.append(np.array(holczer_data[use,1], dtype='int'))
            holczer_tts.append(holczer_data[use,2] + holczer_data[use,3]/24/60 + 67)
            holczer_pers.append(np.median(holczer_tts[npl][1:] - holczer_tts[npl][:-1]))
            
        else:
            holczer_inds.append(None)
            holczer_tts.append(None)
            holczer_pers.append(np.nan)
            
    holczer_pers = np.asarray(holczer_pers)
    
    
# synthetic "Holczer" TTVs are approximated as ground truth + Student-t2 noise
if MISSION == 'Simulated':
    holczer_inds = []
    holczer_tts  = []
    holczer_pers = []
    
    for npl, p in enumerate(planets):
        # read in the "ground truth" TTVs
        fname_in = DOWNLOAD_DIR + 'TTVs/' + TARGET + '_0{0}_sim_ttvs.txt'.format(npl)
        data_in  = np.loadtxt(fname_in).swapaxes(0,1)
    
        inds = np.array(data_in[0], dtype='int')
        tts_true  = np.array(data_in[1], dtype='float')
        
        # add some noise and reject transits without photometry cover
        if len(tts_true) > 20:
            tts_noisy = tts_true + stats.t.rvs(df=2, size=len(tts_true))*p.duration/3
        else:
            tts_noisy = tts_true + np.random.normal(size=len(tts_true))*p.duration/3
        
        keep = np.zeros(len(tts_noisy), dtype='bool')
        
        for i, t0 in enumerate(tts_noisy):
            for j, scd in enumerate(sc_data):
                if np.min(np.abs(scd.time - t0)) < p.duration:
                    keep[i] = True
            for j, lcd in enumerate(lc_data):
                if np.min(np.abs(lcd.time - t0)) < p.duration:
                    keep[i] = True
        
        holczer_inds.append(inds[keep])
        holczer_tts.append(tts_noisy[keep])
        holczer_pers.append(p.period)
        
        
    holczer_pers = np.array(holczer_pers)


# smooth and interpolate Holczer+ 2016 TTVs where they exist

for npl in range(NPL):
    if np.isfinite(holczer_pers[npl]):
        # fit a linear ephemeris 
        pfit  = poly.polyfit(holczer_inds[npl], holczer_tts[npl], 1)
        ephem = poly.polyval(holczer_inds[npl], pfit)
        
        
        # put fitted epoch in range (TIME_START, TIME_START + PERIOD)
        hepoch, hper = pfit

        if hepoch < TIME_START:
            adj = 1 + (TIME_START - hepoch)//hper
            hepoch += adj*hper       

        if hepoch > (TIME_START + hper):
            adj = (hepoch - TIME_START)//hper
            hepoch -= adj*hper      

        hephem = np.arange(hepoch, TIME_END, hper)        
        hinds  = np.array(np.round((hephem-hepoch)/hper),dtype='int')
        
        
        # calculate OMC and flag outliers
        xtime = np.copy(holczer_tts[npl])
        yomc  = (holczer_tts[npl] - ephem)

        ymed = ndimage.median_filter(yomc, size=5, mode='mirror')
        out  = np.abs(yomc-ymed)/astropy.stats.mad_std(yomc-ymed) > 3.0
        
        
        # estimate TTV signal with a regularized Matern-3/2 GP
        holczer_model = omc.matern32_model(xtime[~out], yomc[~out], hephem)

        with holczer_model:
            holczer_map = pmx.optimize()


        htts = hephem + holczer_map['pred']

        holczer_inds[npl] = np.copy(hinds)
        holczer_tts[npl] = np.copy(htts)

        # plot the results
        plt.figure(figsize=(12,4))
        plt.plot(xtime[~out], yomc[~out]*24*60, 'o', c='grey', label="Holczer")
        plt.plot(xtime[out], yomc[out]*24*60, 'rx')
        plt.plot(hephem, (htts-hephem)*24*60, 'k+', label="Interpolation")
        plt.xlabel("Time [BJKD]", fontsize=20)
        plt.ylabel("O-C [min]", fontsize=20)
        plt.legend(fontsize=12)
        plt.savefig(FIGURE_DIR + TARGET + '_ttvs_holczer_{0}.png'.format(npl), bbox_inches='tight')
        if ~iplot: plt.close()


# check if Holczer TTVs exist, and if so, replace the linear ephemeris

for npl, p in enumerate(planets):
    match = np.isclose(holczer_pers, p.period, rtol=0.1, atol=DURS.max())
    
    if np.sum(match) > 1:
        raise ValueError("Something has gone wrong matching periods between DR25 and Holczer+ 2016")
        
    if np.sum(match) == 1:
        loc = np.squeeze(np.where(match))
    
        hinds = holczer_inds[loc]
        htts  = holczer_tts[loc]
        
        # first update to Holczer ephemeris
        epoch, period = poly.polyfit(hinds, htts, 1)
        
        p.epoch = np.copy(epoch)
        p.period = np.copy(period)
        p.tts = np.arange(p.epoch, TIME_END, p.period)
        
        for i, t0 in enumerate(p.tts):
            for j, tH in enumerate(htts):
                if np.abs(t0-tH)/p.period < 0.25:
                    p.tts[i] = tH

    else:
        pass


# plot the OMC TTVs
fig, axes = plt.subplots(NPL, figsize=(12,3*NPL))
if NPL == 1: axes = [axes]

for npl, p in enumerate(planets):
    xtime = poly.polyval(p.index, poly.polyfit(p.index, p.tts, 1))
    yomc  = (p.tts - xtime)*24*60
    
    axes[npl].plot(xtime, yomc, '.', c='C{0}'.format(npl))
    axes[npl].set_ylabel('O-C [min]', fontsize=20)
axes[NPL-1].set_xlabel('Time [BJKD]', fontsize=20)
axes[0].set_title(TARGET, fontsize=20)
plt.savefig(FIGURE_DIR + TARGET + '_ttvs_initial.png', bbox_inches='tight')
if ~iplot: plt.close()


######################
# - 1ST DETRENDING - #
######################

print("\nDetrending lightcurves (1st pass)...\n")


# Detrend the lightcurves

# long cadence data
break_tolerance = np.max([int(DURS.min()/(LCIT/60/24)*5/2), 13])
min_period = 1.0

for i, lcd in enumerate(lc_data):
    print("QUARTER {}".format(lcd.quarter[0]))
    
    qmask = lk.KeplerQualityFlags.create_quality_mask(lcd.quality, bitmask='default')
    lcd.remove_flagged_cadences(qmask)
    
    # make transit mask
    lcd.mask = np.zeros(len(lcd.time), dtype='bool')
    for npl, p in enumerate(planets):
        lcd.mask += detrend.make_transitmask(lcd.time, p.tts, np.max([1/24,1.5*p.duration]))
    
    lcd.clip_outliers(kernel_size=13, sigma_upper=5, sigma_lower=5, mask=lcd.mask)
    lcd.clip_outliers(kernel_size=13, sigma_upper=5, sigma_lower=1000, mask=None)
    
    try:
        lcd = detrend.flatten_with_gp(lcd, break_tolerance, min_period)
    except:
        warnings.warn("Initial detrending model failed...attempting to refit without exponential ramp component")
        try:
            lcd = detrend.flatten_with_gp(lcd, break_tolerance, min_period, correct_ramp=False)
        except:
            warnings.warn("Detrending with RotationTerm failed...attempting to detrend with SHOTerm")
            lcd = detrend.flatten_with_gp(lcd, break_tolerance, min_period, kterm='SHOTerm', correct_ramp=False)
            
            
if len(lc_data) > 0:
    lc = detrend.stitch(lc_data)
else:
    lc = None

# short cadence data
break_tolerance = np.max([int(DURS.min()/(SCIT/3600/24)*5/2), 91])
min_period = 1.0

for i, scd in enumerate(sc_data):
    print("QUARTER {}".format(scd.quarter[0]))
    
    qmask = lk.KeplerQualityFlags.create_quality_mask(scd.quality, bitmask='default')
    scd.remove_flagged_cadences(qmask)
    
    # make transit mask
    scd.mask = np.zeros(len(scd.time), dtype='bool')
    for npl, p in enumerate(planets):
        scd.mask += detrend.make_transitmask(scd.time, p.tts, np.max([1/24,1.5*p.duration]))
    
    scd.clip_outliers(kernel_size=13, sigma_upper=5, sigma_lower=5, mask=scd.mask)
    scd.clip_outliers(kernel_size=13, sigma_upper=5, sigma_lower=1000, mask=None)
    
    try:
        scd = detrend.flatten_with_gp(scd, break_tolerance, min_period)
    except:
        warnings.warn("Initial detrending model failed...attempting to refit without exponential ramp component")
        try:
            scd = detrend.flatten_with_gp(scd, break_tolerance, min_period, correct_ramp=False)
        except:
            warnings.warn("Detrending with RotationTerm failed...attempting to detrend with SHOTerm")
            scd = detrend.flatten_with_gp(scd, break_tolerance, min_period, kterm='SHOTerm', correct_ramp=False)
            
            
if len(sc_data) > 0:
    sc = detrend.stitch(sc_data)
else:
    sc = None


# Make wide masks that track each planet individually
# These masks have width 2.5 transit durations, which is probably wider than the masks used for detrending

if sc is not None:
    sc_mask = np.zeros((NPL,len(sc.time)),dtype='bool')
    for npl, p in enumerate(planets):
        sc_mask[npl] = detrend.make_transitmask(sc.time, p.tts, np.max([2/24,2.5*p.duration]))
        
    sc.mask = sc_mask.sum(axis=0) > 0

else:
    sc_mask = None

    
if lc is not None:
    lc_mask = np.zeros((NPL,len(lc.time)),dtype='bool')
    for npl, p in enumerate(planets):
        lc_mask[npl] = detrend.make_transitmask(lc.time, p.tts, np.max([2/24,2.5*p.duration]))
        
    lc.mask = lc_mask.sum(axis=0) > 0

else:
    lc_mask = None


# Flag high quality transits (quality = 1)
# Good transits must have  at least 50% photometry coverage in/near transit

for npl, p in enumerate(planets):
    count_expect_lc = int(np.ceil(p.duration/lcit))
    count_expect_sc = int(np.ceil(p.duration/scit))
        
    quality = np.zeros(len(p.tts), dtype='bool')
    
    for i, t0 in enumerate(p.tts):
        
        if sc is not None:
            in_sc = np.abs(sc.time - t0)/p.duration < 0.5
            near_sc = np.abs(sc.time - t0)/p.duration < 1.5
            
            qual_in = np.sum(in_sc) > 0.5*count_expect_sc
            qual_near = np.sum(near_sc) > 1.5*count_expect_sc
            
            quality[i] += qual_in*qual_near
        
        
        if lc is not None:
            in_lc = np.abs(lc.time - t0)/p.duration < 0.5
            near_lc = np.abs(lc.time - t0)/p.duration < 1.5
            
            qual_in = np.sum(in_lc) > 0.5*count_expect_lc
            qual_near = np.sum(near_lc) > 1.5*count_expect_lc
            
            quality[i] += qual_in*qual_near
            
    
    p.quality = np.copy(quality)


# Identify overlapping transits
dur_max = np.max(DURS)
overlap = []

for i in range(NPL):
    overlap.append(np.zeros(len(planets[i].tts), dtype='bool'))
    
    for j in range(NPL):
        if i != j:
            for ttj in planets[j].tts:
                overlap[i] += np.abs(planets[i].tts - ttj)/dur_max < 1.5
                
    planets[i].overlap = np.copy(overlap[i])


# Count up transits and calculate initial fixed transit times
num_transits = np.zeros(NPL)
transit_inds = []
fixed_tts = []

for npl, p in enumerate(planets):
    transit_inds.append(np.array((p.index - p.index.min())[p.quality], dtype='int'))
    fixed_tts.append(np.copy(p.tts)[p.quality])
    
    num_transits[npl] = len(transit_inds[npl])
    transit_inds[npl] -= transit_inds[npl].min()


# Grab data near transits, going quarter-by-quarter
all_time = [None]*18
all_flux = [None]*18
all_error = [None]*18
all_dtype = ['none']*18

lc_flux = []
sc_flux = []


for q in range(18):
    if sc is not None:
        if np.isin(q, sc.quarter):
            use = (sc.mask)*(sc.quarter == q)

            if np.sum(use) > 45:
                all_time[q] = sc.time[use]
                all_flux[q] = sc.flux[use]
                all_error[q] = sc.error[use]
                all_dtype[q] = 'short'

                sc_flux.append(sc.flux[use])
                
            else:
                all_dtype[q] = 'short_no_transits'

    
    if lc is not None:
        if np.isin(q, lc.quarter):
            use = (lc.mask)*(lc.quarter == q)

            if np.sum(use) > 5:
                all_time[q] = lc.time[use]
                all_flux[q] = lc.flux[use]
                all_error[q] = lc.error[use]
                all_dtype[q] = 'long'

                lc_flux.append(lc.flux[use])
                
            else:
                all_dtype[q] = 'long_no_transits'
                
                
                
# check which quarters have coverage
good = (np.array(all_dtype) == 'short') + (np.array(all_dtype) == 'long')
quarters = np.arange(18)[good]
nq = len(quarters)


# make some linear flux arrays (for convenience use laster)
try: sc_flux_lin = np.hstack(sc_flux)
except: sc_flux_lin = np.array([])
    
try: lc_flux_lin = np.hstack(lc_flux)
except: lc_flux_lin = np.array([])
    
try:
    good_flux = np.hstack([sc_flux_lin, lc_flux_lin])
except:
    try:
        good_flux = np.hstack(sc_flux)
    except:
        good_flux = np.hstack(lc_flux)
        
        
# set oversampling factors and expoure times
oversample = np.zeros(18, dtype='int')
texp = np.zeros(18)

oversample[np.array(all_dtype)=='short'] = 1
oversample[np.array(all_dtype)=='long'] = 15

texp[np.array(all_dtype)=='short'] = scit
texp[np.array(all_dtype)=='long'] = lcit


# Pull basic transit parameters
periods = np.zeros(NPL)
epochs  = np.zeros(NPL)
depths  = np.zeros(NPL)
durs    = np.zeros(NPL)
impacts = np.zeros(NPL)

for npl, p in enumerate(planets):
    periods[npl] = p.period
    epochs[npl]  = p.epoch
    depths[npl]  = p.depth
    durs[npl]    = p.duration
    impacts[npl] = p.impact


# Use Legendre polynomials over transit times for better orthogonality; "x" is in the range (-1,1)
# The current version of the code only uses 1st order polynomials, but 2nd and 3rd are retained for posterity
Leg0 = []
Leg1 = []
Leg2 = []
Leg3 = []
t = []

# this assumes a baseline in the range (TIME_START,TIME_END)
for npl, p in enumerate(planets):    
    t.append(p.epoch + transit_inds[npl]*p.period)
    x = 2*(t[npl]-TIME_START)/(TIME_END-TIME_START) - 1

    Leg0.append(np.ones_like(x))
    Leg1.append(x.copy())
    Leg2.append(0.5*(3*x**2 - 1))
    Leg3.append(0.5*(5*x**3 - 3*x))

print("")
print("cumulative runtime = ", int(timer() - global_start_time), "s")
print("")


##########################
# - LIGHTCURVE FITTING - #
##########################

# Fit transit SHAPE model
print('\nFitting transit SHAPE model...\n')

with pm.Model() as shape_model:
    # planetary parameters
    log_r = pm.Uniform('log_r', lower=np.log(1e-5), upper=np.log(0.99), shape=NPL, testval=np.log(np.sqrt(depths)))
    r = pm.Deterministic('r', T.exp(log_r))    
    b = pm.Uniform('b', lower=0., upper=1., shape=NPL)
    
    log_dur = pm.Normal('log_dur', mu=np.log(durs), sd=5.0, shape=NPL)
    dur = pm.Deterministic('dur', T.exp(log_dur))
    
    # polynomial TTV parameters    
    C0 = pm.Normal('C0', mu=0.0, sd=durs/2, shape=NPL)
    C1 = pm.Normal('C1', mu=0.0, sd=durs/2, shape=NPL)
    
    transit_times = []
    for npl in range(NPL):
        transit_times.append(pm.Deterministic('tts_{0}'.format(npl), 
                                              fixed_tts[npl] + C0[npl]*Leg0[npl] + C1[npl]*Leg1[npl]))
    
    
    # set up stellar model and planetary orbit
    starrystar = exo.LimbDarkLightCurve([U1,U2])
    orbit = exo.orbits.TTVOrbit(transit_times=transit_times, transit_inds=transit_inds, 
                                b=b, ror=r, duration=dur)
    
    # track period and epoch
    T0 = pm.Deterministic('T0', orbit.t0)
    P  = pm.Deterministic('P', orbit.period)
    
    
    # nuissance parameters
    flux0 = pm.Normal('flux0', mu=np.mean(good_flux), sd=np.std(good_flux), shape=len(quarters))
    log_jit = pm.Normal('log_jit', mu=np.log(np.var(good_flux)/10), sd=10, shape=len(quarters))
    

    # now evaluate the model for each quarter
    light_curves = [None]*nq
    model_flux = [None]*nq
    flux_err = [None]*nq
    obs = [None]*nq
    
    for j, q in enumerate(quarters):
        # calculate light curves
        light_curves[j] = starrystar.get_light_curve(orbit=orbit, r=r, t=all_time[q], 
                                                     oversample=oversample[j], texp=texp[j])
        
        model_flux[j] = pm.math.sum(light_curves[j], axis=-1) + flux0[j]*T.ones(len(all_time[q]))
        flux_err[j] = T.sqrt(np.mean(all_error[q])**2 + T.exp(log_jit[j]))/np.sqrt(2)
        
        obs[j] = pm.Normal('obs_{0}'.format(j), 
                           mu=model_flux[j], 
                           sd=flux_err[j], 
                           observed=all_flux[q])


# find maximum a posteriori (MAP) solution
with shape_model:
    shape_map = shape_model.test_point
    shape_map = pmx.optimize(start=shape_map, vars=[flux0, log_jit])
    shape_map = pmx.optimize(start=shape_map, vars=[b, r, dur])
    shape_map = pmx.optimize(start=shape_map, vars=[C0, C1])
    shape_map = pmx.optimize(start=shape_map)

    
# grab transit times and ephemeris
shape_transit_times = []
shape_ephemeris = []

for npl, p in enumerate(planets):
    shape_transit_times.append(shape_map['tts_{0}'.format(npl)])
    shape_ephemeris.append(shape_map['P'][npl]*transit_inds[npl] + shape_map['T0'][npl])

# update parameter values
periods = np.atleast_1d(shape_map['P'])
epochs  = np.atleast_1d(shape_map['T0'])
depths  = np.atleast_1d(get_transit_depth(shape_map['r'], shape_map['b']))
durs    = np.atleast_1d(shape_map['dur'])
impacts = np.atleast_1d(shape_map['b'])
rors    = np.atleast_1d(shape_map['r'])

for npl, p in enumerate(planets):
    p.period   = periods[npl]
    p.epoch    = epochs[npl]
    p.depth    = depths[npl]
    p.duration = durs[npl]
    p.impact   = impacts[npl]

print("")
print("cumulative runtime = ", int(timer() - global_start_time), "s")
print("")


# Fit TTVs via cross-correlation (aka "slide" ttvs)
print('\nFitting TTVs..\n')

slide_transit_times = []
slide_error = []

t_all = np.array(np.hstack(all_time), dtype='float')
f_all = np.array(np.hstack(all_flux), dtype='float')

for npl, p in enumerate(planets):
    print("\nPLANET", npl)
    
    slide_transit_times.append([])
    slide_error.append([])
    
    # create template transit
    starrystar = exo.LimbDarkLightCurve([U1,U2])
    orbit  = exo.orbits.KeplerianOrbit(t0=0, period=p.period, b=p.impact, ror=rors[npl], duration=p.duration)

    gridstep     = scit/2
    slide_offset = 1.0
    delta_chisq  = 2.0

    template_time = np.arange(-(0.02+p.duration)*(slide_offset+1.6), (0.02+p.duration)*(slide_offset+1.6), gridstep)
    template_flux = 1.0 + starrystar.get_light_curve(orbit=orbit, r=rors[npl], t=template_time).sum(axis=-1).eval()
    
    # empty lists to hold new transit time and uncertainties
    tts = -99*np.ones_like(shape_transit_times[npl])
    err = -99*np.ones_like(shape_transit_times[npl])
    
    for i, t0 in enumerate(shape_transit_times[npl]):
        #print(i, np.round(t0,2))
        if ~p.overlap[p.quality][i]:
        
            # grab flux near each non-overlapping transit
            use = np.abs(t_all - t0)/p.duration < 2.5
            mask = np.abs(t_all - t0)/p.duration < 1.0

            t_ = t_all[use]
            f_ = f_all[use]
            m_ = mask[use]
            
            # remove any residual out-of-transit trend
            try:
                trend = poly.polyval(t_, poly.polyfit(t_[~m_], f_[~m_], 1))
            
                f_ /= trend
                e_ = np.ones_like(f_)*np.std(f_[~m_])
                
            except:
                e_ = np.ones_like(f_)*np.std(f_)
            
            # slide along transit time vector and calculate chisq
            tc_vector = t0 + np.arange(-p.duration*slide_offset, p.duration*slide_offset, gridstep)
            chisq_vector = np.zeros_like(tc_vector)

            for j, tc in enumerate(tc_vector):
                y_ = np.interp(t_-tc, template_time, template_flux)
                chisq_vector[j] = np.sum((f_ - y_)**2/e_**2)

            chisq_vector = boxcar_smooth(chisq_vector, winsize=7)

            # grab points near minimum chisq
            delta_chisq = 1
            
            loop = True
            while loop:
                # incrememnt delta_chisq and find minimum
                delta_chisq += 1
                min_chisq = chisq_vector.min()
                
                # grab the points near minimum
                tcfit = tc_vector[chisq_vector < min_chisq+delta_chisq]
                x2fit = chisq_vector[chisq_vector < min_chisq+delta_chisq]

                # eliminate points far from the local minimum
                spacing = np.median(tcfit[1:]-tcfit[:-1])
                faraway = np.abs(tcfit-np.median(tcfit))/spacing > 1 + len(tcfit)/2
                
                tcfit = tcfit[~faraway]
                x2fit = x2fit[~faraway]
                
                # check for stopping conditions
                if len(x2fit) >= 3:
                    loop = False
                    
                if delta_chisq >= 9:
                    loop = False
                    
            # fit a parabola around the minimum (need at least 3 pts)
            if len(tcfit) < 3:
                #print("TOO FEW POINTS")
                tts[i] = np.nan
                err[i] = np.nan

            else:
                quad_coeffs = np.polyfit(tcfit, x2fit, 2)
                quadfit = np.polyval(quad_coeffs, tcfit)
                qtc_min = -quad_coeffs[1]/(2*quad_coeffs[0])
                qx2_min = np.polyval(quad_coeffs, qtc_min)
                qtc_err = np.sqrt(1/quad_coeffs[0])

                # here's the fitted transit time
                tts[i] = np.mean([qtc_min,np.median(tcfit)])
                err[i] = qtc_err*1.0

                # check that the fit is well-conditioned (ie. a negative t**2 coefficient)
                if quad_coeffs[0] <= 0.0:
                    #print("INVERTED PARABOLA")
                    tts[i] = np.nan
                    err[i] = np.nan

                # check that the recovered transit time is within the expected range
                if (tts[i] < tcfit.min()) or (tts[i] > tcfit.max()):
                    #print("T0 OUT OF BOUNDS")
                    tts[i] = np.nan
                    err[i] = np.nan

            # show plots
            if ~np.isnan(tts[i]):
                do_plots = False
                    
                if do_plots:
                    fig, ax = plt.subplots(1,2, figsize=(10,3))

                    ax[0].plot(t_-tts[i], f_, 'ko')
                    ax[0].plot((t_-tts[i])[m_], f_[m_], 'o', c='C{0}'.format(npl))
                    ax[0].plot(template_time, template_flux, c='C{0}'.format(npl), lw=2)

                    ax[1].plot(tcfit, x2fit, 'ko')
                    ax[1].plot(tcfit, quadfit, c='C{0}'.format(npl), lw=3)
                    ax[1].axvline(tts[i], color='k', ls='--', lw=2)
                    
                    if ~iplot: plt.close()
                    
                    
        else:
            #print("OVERLAPPING TRANSITS")
            tts[i] = np.nan
            err[i] = np.nan
        
    slide_transit_times[npl] = np.copy(tts)
    slide_error[npl] = np.copy(err)

# flag transits for which the cross-correlation method failed
for npl, p in enumerate(planets):
    bad = np.isnan(slide_transit_times[npl]) + np.isnan(slide_error[npl])
    bad += slide_error[npl] > 8*np.nanmedian(slide_error[npl])
    
    slide_transit_times[npl][bad] = shape_transit_times[npl][bad]
    slide_error[npl][bad] = np.nan
    
    
refit = []

for npl in range(NPL):
    refit.append(np.isnan(slide_error[npl]))
    
    # if every slide fit worked, randomly select a pair of transits for refitting
    # this is easier than tracking the edge cases -- we'll use the slide ttvs in the final vector anyway
    if np.all(~refit[npl]):
        refit[npl][np.random.randint(len(refit[npl]), size=2)] = True


# plot the OMC TTVs
fig, axes = plt.subplots(NPL, figsize=(12,3*NPL))
if NPL == 1: axes = [axes]

for npl, p in enumerate(planets):
    ephem = poly.polyval(transit_inds[npl], poly.polyfit(transit_inds[npl], slide_transit_times[npl], 1))
    
    xtime = slide_transit_times[npl]
    yomc  = (slide_transit_times[npl] - ephem)*24*60
    yerr  = slide_error[npl]*24*60
    
    good = ~np.isnan(slide_error[npl])
    
    axes[npl].plot(xtime[~good], yomc[~good], 'd', color='lightgrey')
    axes[npl].errorbar(xtime[good], yomc[good], yerr=yerr[good], fmt='.', color='C{0}'.format(npl))
    axes[npl].set_ylabel("O-C [min]", fontsize=20)
axes[NPL-1].set_xlabel("Time [BJKD]", fontsize=20)
axes[0].set_title(TARGET, fontsize=20)
plt.savefig(FIGURE_DIR + TARGET + '_ttvs_slide.png', bbox_inches='tight')
if ~iplot: plt.close()


print("")
print("cumulative runtime = ", int(timer() - global_start_time), "s")
print("")


# Fit MAP INDEPENDENT TTVs (only refit transits for which the cross-correlation method failed)

if sc is not None:
    sc_map_mask = np.zeros((NPL,len(sc.time)),dtype='bool')
    for npl, p in enumerate(planets):
        tts = slide_transit_times[npl][refit[npl]]
        sc_map_mask[npl] = detrend.make_transitmask(sc.time, tts, np.max([2/24,2.5*p.duration]))
        
    sc_map_mask = sc_map_mask.sum(axis=0) > 0

else:
    sc_map_mask = None

    
if lc is not None:
    lc_map_mask = np.zeros((NPL,len(lc.time)),dtype='bool')
    for npl, p in enumerate(planets):
        tts = slide_transit_times[npl][refit[npl]]
        lc_map_mask[npl] = detrend.make_transitmask(lc.time, tts, np.max([2/24,2.5*p.duration]))
        
    lc_map_mask = lc_map_mask.sum(axis=0) > 0

else:
    lc_map_mask = None

    
# grab data near transits for each quarter
map_time = [None]*18
map_flux = [None]*18
map_error = [None]*18
map_dtype = ['none']*18

for q in range(18):
    if sc is not None:
        if np.isin(q, sc.quarter):
            use = (sc_map_mask)*(sc.quarter == q)

            if np.sum(use) > 45:
                map_time[q] = sc.time[use]
                map_flux[q] = sc.flux[use]
                map_error[q] = sc.error[use]
                map_dtype[q] = 'short'
                
            else:
                map_dtype[q] = 'short_no_transits'

    
    if lc is not None:
        if np.isin(q, lc.quarter):
            use = (lc_map_mask)*(lc.quarter == q)

            if np.sum(use) > 5:
                map_time[q] = lc.time[use]
                map_flux[q] = lc.flux[use]
                map_error[q] = lc.error[use]
                map_dtype[q] = 'long'
                
            else:
                map_dtype[q] = 'long_no_transits'
                
map_quarters = np.arange(18)[(np.array(map_dtype) == 'short') + (np.array(map_dtype) == 'long')]


with pm.Model() as indep_model:
    # transit times
    tt_offset = []
    map_tts  = []
    map_inds = []
    
    for npl in range(NPL):
        use = np.copy(refit[npl])
        
        tt_offset.append(pm.Normal('tt_offset_{0}'.format(npl), mu=0, sd=1, shape=np.sum(use)))

        map_tts.append(pm.Deterministic('tts_{0}'.format(npl),
                                        shape_transit_times[npl][use] + tt_offset[npl]*durs[npl]/3))
        
        map_inds.append(transit_inds[npl][use])
        
    # set up stellar model and planetary orbit
    starrystar = exo.LimbDarkLightCurve([U1,U2])
    orbit  = exo.orbits.TTVOrbit(transit_times=map_tts, transit_inds=map_inds, 
                                 period=periods, b=impacts, ror=rors, duration=durs)
    
    # nuissance parameters
    flux0 = pm.Normal('flux0', mu=np.mean(good_flux), sd=np.std(good_flux), shape=len(map_quarters))
    log_jit = pm.Normal('log_jit', mu=np.log(np.var(good_flux)/10), sd=10, shape=len(map_quarters))
        
    # now evaluate the model for each quarter
    light_curves = [None]*len(map_quarters)
    model_flux = [None]*len(map_quarters)
    flux_err = [None]*len(map_quarters)
    obs = [None]*len(map_quarters)
    
    for j, q in enumerate(map_quarters):
        # calculate light curves
        light_curves[j] = starrystar.get_light_curve(orbit=orbit, r=rors, t=map_time[q], 
                                                     oversample=oversample[j], texp=texp[j])
        
        model_flux[j] = pm.math.sum(light_curves[j], axis=-1) + flux0[j]*T.ones(len(map_time[q]))
        flux_err[j] = T.sqrt(np.mean(map_error[q])**2 + T.exp(log_jit[j]))/np.sqrt(2)
        
        obs[j] = pm.Normal('obs_{0}'.format(j), 
                           mu=model_flux[j], 
                           sd=flux_err[j], 
                           observed=map_flux[q])


with indep_model:
    indep_map = indep_model.test_point
    indep_map = pmx.optimize(start=indep_map, vars=[flux0, log_jit])
    
    for npl in range(NPL):
        indep_map = pmx.optimize(start=indep_map, vars=[tt_offset[npl]])
        
    indep_map = pmx.optimize(start=indep_map)

indep_transit_times = []
indep_error = []
indep_ephemeris = []
full_indep_ephemeris = []

for npl, p in enumerate(planets):
    indep_transit_times.append(np.copy(slide_transit_times[npl]))
    indep_error.append(np.copy(slide_error[npl]))
    
    replace = np.isnan(slide_error[npl])
    
    if np.any(replace):
        indep_transit_times[npl][replace] = indep_map['tts_{0}'.format(npl)]

    pfit = poly.polyfit(transit_inds[npl], indep_transit_times[npl], 1)

    indep_ephemeris.append(poly.polyval(transit_inds[npl], pfit))
    full_indep_ephemeris.append(poly.polyval(p.index, pfit))

    if np.any(replace):
        indep_error[npl][replace] = np.std(indep_transit_times[npl] - indep_ephemeris[npl])

    
fig, axes = plt.subplots(NPL, figsize=(12,3*NPL))
if NPL == 1: axes = [axes]

for npl, p in enumerate(planets):
    xtime = indep_transit_times[npl]
    yomc  = (indep_transit_times[npl] - indep_ephemeris[npl])*24*60
    yerr  = (indep_error[npl])*24*60
    
    axes[npl].errorbar(xtime, yomc, yerr=yerr, fmt='.', c='C{0}'.format(npl))
    axes[npl].set_ylabel("O-C [min]", fontsize=20)
axes[NPL-1].set_xlabel("Time [BJKD]", fontsize=20)
axes[0].set_title(TARGET, fontsize=20)
plt.savefig(FIGURE_DIR + TARGET + '_ttvs_indep.png', bbox_inches='tight')
if ~iplot: plt.close()

print("")
print("cumulative runtime = ", int(timer() - global_start_time), "s")
print("")


###########################
# - OMC MODEL SELECTION - #
###########################

print("\nIdentifying best OMC model...\n")


# search for periodic signals
print("...searching for periodic signals")

indep_freqs = []
indep_faps = []

for npl, p in enumerate(planets):
    # grab data
    xtime = indep_ephemeris[npl]
    yomc  = indep_transit_times[npl] - indep_ephemeris[npl]

    ymed = boxcar_smooth(ndimage.median_filter(yomc, size=5, mode='mirror'), winsize=5)
    out  = np.abs(yomc-ymed)/astropy.stats.mad_std(yomc-ymed) > 5.0
    
    # search for a periodic component
    peakfreq = np.nan
    peakfap = 1.0
    
    if NPL == 1: fap = 0.1
    elif NPL > 1: fap = 0.99
    
    if np.sum(~out) > 8:
        try:
            xf, yf, freqs, faps = LS_estimator(xtime[~out], yomc[~out], fap=fap)

            if len(freqs) > 0:
                if freqs[0] > xf.min():
                    peakfreq = freqs[0]
                    peakfap = faps[0]
                    
        except:
            pass
    
    indep_freqs.append(peakfreq)
    indep_faps.append(peakfap)

omc_freqs = []
omc_faps = []

# for single planet systems, use the direct LS output
if NPL == 1:
    if np.isnan(indep_freqs[0]):
        omc_freqs.append(None)
        omc_faps.append(None)
    else:
        omc_freqs.append(indep_freqs[0])
        omc_faps.append(indep_faps[0])
    

# for multiplanet systems, check if any statistically marginal frequencies match between planets
elif NPL > 1:
    
    for i in range(NPL):
        # save any low FAP frequencies
        if indep_faps[i] < 0.1:
            omc_freqs.append(indep_freqs[i])
            omc_faps.append(indep_faps[i])
            
        # check if the LS frequency is close to that of any other planet
        else:
            close = False
            
            df_min = 1/(indep_ephemeris[i].max() - indep_ephemeris[i].min())
            
            for j in range(i+1, NPL):
                # delta-freq (LS) between two planets
                df_ij = np.abs(indep_freqs[i]-indep_freqs[j])
                
                if df_ij < df_min:
                    close = True
                    
            if close:
                omc_freqs.append(indep_freqs[i])
                omc_faps.append(indep_faps[i])
                
            else:
                omc_freqs.append(None)
                omc_faps.append(None)

omc_pers = []

for npl in range(NPL):
    print("\nPLANET", npl)
    
    # roughly model OMC based on single frequency sinusoid (if found)
    if omc_freqs[npl] is not None:
        print("periodic signal found at P =", int(1/omc_freqs[npl]), "d")
        
        # store frequency
        omc_pers.append(1/omc_freqs[npl])
        
        # grab data and plot
        xtime = indep_ephemeris[npl]
        yomc  = indep_transit_times[npl] - indep_ephemeris[npl]
        LS = LombScargle(xtime, yomc)
        
        plt.figure(figsize=(12,3))
        plt.plot(xtime, yomc*24*60, 'o', c='lightgrey')
        plt.plot(xtime, LS.model(xtime, omc_freqs[npl])*24*60, c='C{0}'.format(npl), lw=3)
        if ~iplot: plt.close()
    
    else:
        print("no sigificant periodic component found")
        omc_pers.append(2*(indep_ephemeris[npl].max()-indep_ephemeris[npl].min()))


# determine best OMC model
print("...running model selection routine")

quick_transit_times = []
full_quick_transit_times = []

outlier_prob = []
outlier_class = []

for npl, p in enumerate(planets):
    print("\nPLANET", npl)
    
    # grab data
    xtime = indep_ephemeris[npl]
    yomc  = indep_transit_times[npl] - indep_ephemeris[npl]

    ymed = boxcar_smooth(ndimage.median_filter(yomc, size=5, mode='mirror'), winsize=5)
    out  = np.abs(yomc-ymed)/astropy.stats.mad_std(yomc-ymed) > 5.0
    
    
    # compare various models
    aiclist = []
    biclist = []
    fgplist = []
    outlist = []
    
    if np.sum(~out) >= 16: max_polyorder = 3
    elif np.sum(~out) >= 8: max_polyorder = 2
    else: max_polyorder = 1
    
    for polyorder in range(-1, max_polyorder+1):
        if polyorder == -1:
            omc_model = omc.matern32_model(xtime[~out], yomc[~out], xtime)
        elif polyorder == 0:
            omc_model = omc.sin_model(xtime[~out], yomc[~out], omc_pers[npl], xtime)
        elif polyorder >= 1:
            omc_model = omc.poly_model(xtime[~out], yomc[~out], polyorder, xtime)

        with omc_model:
            omc_map = omc_model.test_point
            omc_map = pmx.optimize(start=omc_map)
            omc_trace = pmx.sample(tune=8000, draws=2000, start=omc_map, chains=2, target_accept=0.95)

        omc_trend = np.nanmedian(omc_trace['pred'], 0)
        residuals = yomc - omc_trend

        plt.figure(figsize=(12,3))
        plt.plot(xtime, yomc*24*60, '.', c='lightgrey')
        plt.plot(xtime[out], yomc[out]*24*60, 'rx')
        plt.plot(xtime, omc_trend*24*60, c='C{0}'.format(npl), lw=2)
        plt.xlabel("Time [BJKD]", fontsize=16)
        plt.ylabel("O-C [min]", fontsize=16)
        if ~iplot: plt.close()

        # flag outliers via mixture model of the residuals
        mix_model = omc.mix_model(residuals)

        with mix_model:
            mix_trace = pmx.sample(tune=8000, draws=2000, chains=1, target_accept=0.95)

        loc = np.nanmedian(mix_trace['mu'], axis=0)
        scales = np.nanmedian(1/np.sqrt(mix_trace['tau']), axis=0)

        fg_prob, bad = omc.flag_outliers(residuals, loc, scales)
        
        fgplist.append(fg_prob)
        outlist.append(bad)
        
        print("{0} outliers found out of {1} transit times ({2}%)".format(np.sum(bad), len(bad), 
                                                                          np.round(100.*np.sum(bad)/len(bad),1)))
        
        # calculate AIC & BIC
        n = len(yomc)
        
        if polyorder <= 0:
            k = 3
        else:
            k = polyorder + 1
        
        aic = n*np.log(np.sum(residuals[~bad]**2)/np.sum(~bad)) + 2*k
        bic = n*np.log(np.sum(residuals[~bad]**2)/np.sum(~bad)) + k*np.log(n)
        
        aiclist.append(aic)
        biclist.append(bic)
        
        print("AIC:", np.round(aic,1))
        print("BIC:", np.round(bic,1))
        
        
    # choose the best model and recompute
    out = outlist[np.argmin(aiclist)]
    fg_prob = fgplist[np.argmin(aiclist)]
    polyorder = np.argmin(aiclist) - 1
    xt_predict = full_indep_ephemeris[npl]

    if polyorder == -1:
        omc_model = omc.matern32_model(xtime[~out], yomc[~out], xt_predict)
    elif polyorder == 0:
        omc_model = omc.sin_model(xtime[~out], yomc[~out], omc_pers[npl], xt_predict)
    elif polyorder >= 1:
        omc_model = omc.poly_model(xtime[~out], yomc[~out], polyorder, xt_predict)

    with omc_model:
        omc_map = omc_model.test_point
        omc_map = pmx.optimize(start=omc_map)
        omc_trace = pmx.sample(tune=8000, draws=2000, start=omc_map, chains=2, target_accept=0.95)

    full_omc_trend = np.nanmedian(omc_trace['pred'], 0)


    # save the final results
    full_quick_transit_times.append(full_indep_ephemeris[npl] + full_omc_trend)
    quick_transit_times.append(full_quick_transit_times[npl][transit_inds[npl]])
    
    outlier_prob.append(1-fg_prob)
    outlier_class.append(bad)

    
    # plot the final trend and outliers
    plt.figure(figsize=(12,4))
    plt.scatter(xtime, yomc*24*60, c=1-fg_prob, cmap='viridis', label="MAP TTVs")
    plt.plot(xtime[bad], yomc[bad]*24*60, 'rx')
    plt.plot(full_indep_ephemeris[npl], full_omc_trend*24*60, 'k', label="Quick model")
    plt.xlabel("Time [BJKD]", fontsize=20)
    plt.ylabel("O-C [min]", fontsize=20)
    plt.xticks(fontsize=14)
    plt.yticks(fontsize=14)
    plt.legend(fontsize=14, loc='upper right')
    plt.title(TARGET, fontsize=20)
    plt.savefig(FIGURE_DIR + TARGET + '_omc_model_{0}.png'.format(npl), bbox_inches='tight')
    if ~iplot: plt.close()


# make plot
fig, axes = plt.subplots(NPL, figsize=(12,3*NPL))
if NPL == 1: axes = [axes]

for npl, p in enumerate(planets):
    xtime = indep_ephemeris[npl]
    yomc_i = (indep_transit_times[npl] - indep_ephemeris[npl])*24*60    
    yomc_q = (quick_transit_times[npl] - indep_ephemeris[npl])*24*60
    
    axes[npl].plot(xtime, yomc_i, 'o', c='lightgrey')
    axes[npl].plot(xtime, yomc_q, c='C{0}'.format(npl), lw=3)
    axes[npl].set_ylabel('O-C [min]', fontsize=20)

axes[NPL-1].set_xlabel('Time [BJKD]', fontsize=20)
axes[0].set_title(TARGET, fontsize=20)
plt.savefig(FIGURE_DIR + TARGET + '_ttvs_quick.png', bbox_inches='tight')
if ~ iplot: plt.close()


# Estimate TTV scatter w/ uncertainty buffer
ttv_scatter = np.zeros(NPL)
ttv_buffer  = np.zeros(NPL)

for npl in range(NPL):
    # estimate TTV scatter
    ttv_scatter[npl] = astropy.stats.mad_std(indep_transit_times[npl]-quick_transit_times[npl])

    # based on scatter in independent times, set threshold so not even one outlier is expected
    N   = len(transit_inds[npl])
    eta = np.max([3., stats.norm.interval((N-1)/N)[1]])

    ttv_buffer[npl] = eta*ttv_scatter[npl] + lcit


# Update and save TTVs
for npl, p in enumerate(planets):
    # update transit time info in Planet objects
    epoch, period = poly.polyfit(p.index, full_quick_transit_times[npl], 1)

    p.epoch = np.copy(epoch)
    p.period = np.copy(period)
    p.tts = np.copy(full_quick_transit_times[npl])
    
    # save transit timing info to output file
    data_out  = np.vstack([transit_inds[npl],
                           indep_transit_times[npl],
                           quick_transit_times[npl],
                           outlier_prob[npl], 
                           outlier_class[npl]]).swapaxes(0,1)
    fname_out = QUICK_TTV_DIR + TARGET + '_{:02d}'.format(npl) + '_quick.ttvs'
    np.savetxt(fname_out, data_out, fmt=('%1d', '%.8f', '%.8f', '%.8f', '%1d'), delimiter='\t')

print("")
print("cumulative runtime = ", int(timer() - global_start_time), 's')
print("")


# Flag outliers based on transit model
# cadences must be flagged as outliers from *both* the QUICK ttv model and the INDEPENDENT ttv model to be rejected
print("\nFlagging outliers based on transit model...\n")

res_i = []
res_q = []

for j, q in enumerate(quarters):
    print("QUARTER", q)
    
    # grab time and flux data
    if all_dtype[q] == 'long':
        use = lc.quarter == q
        t_ = lc.time[use]
        f_ = lc.flux[use]
        
    elif all_dtype[q] == 'short':
        use = sc.quarter == q
        t_ = sc.time[use]
        f_ = sc.flux[use]
        
    
    # grab transit times for each planet
    wp_i = []
    tts_i = []
    inds_i = []
    
    wp_q = []
    tts_q = []
    inds_q = []
    
    for npl in range(NPL):
        itt = indep_transit_times[npl]
        qtt = quick_transit_times[npl]
        
        use_i = (itt > t_.min())*(itt < t_.max())
        use_q = (qtt > t_.min())*(qtt < t_.max())
        
        if np.sum(use_i) > 0:
            wp_i.append(npl)
            tts_i.append(itt[use_i])
            inds_i.append(transit_inds[npl][use_i] - transit_inds[npl][use_i][0])
            
        if np.sum(use_q) > 0:
            wp_q.append(npl)
            tts_q.append(itt[use_q])
            inds_q.append(transit_inds[npl][use_q] - transit_inds[npl][use_q][0])
            
    
    # first check independent transit times
    if len(tts_i) > 0:
        # set up model
        starrystar = exo.LimbDarkLightCurve([U1,U2])
        orbit  = exo.orbits.TTVOrbit(transit_times=tts_i, transit_inds=inds_i, period=list(periods[wp_i]), 
                                     b=impacts[wp_i], ror=rors[wp_i], duration=durs[wp_i])

        # set oversampling factor
        if all_dtype[q] == 'short':
            oversample = 1
            texp = scit
        elif all_dtype[q] == 'long':
            oversample = 15
            texp = lcit

        # calculate light curves
        light_curves = starrystar.get_light_curve(orbit=orbit, r=rors[wp_i], t=t_, oversample=oversample, texp=texp)
        model_flux = 1.0 + pm.math.sum(light_curves, axis=-1).eval()

    else:
        model_flux = np.ones_like(f_)*np.mean(f_)
    
    # calculate residuals
    res_i.append(f_ - model_flux)
    
    
    # then check matern transit times
    if len(tts_q) > 0:
        # set up model
        starrystar = exo.LimbDarkLightCurve([U1,U2])
        orbit  = exo.orbits.TTVOrbit(transit_times=tts_q, transit_inds=inds_q, period=list(periods[wp_q]), 
                                     b=impacts[wp_q], ror=rors[wp_q], duration=durs[wp_q])

        # set oversampling factor
        if all_dtype[q] == 'short':
            oversample = 1
            texp = scit*1.0
        elif all_dtype[q] == 'long':
            oversample = 15
            texp = lcit*1.0

        # calculate light curves
        light_curves = starrystar.get_light_curve(orbit=orbit, r=rors[wp_q], t=t_, oversample=oversample, texp=texp)
        model_flux = 1.0 + pm.math.sum(light_curves, axis=-1).eval()

    else:
        model_flux = np.ones_like(f_)*np.mean(f_)
    
    # calculate residuals
    res_q.append(f_ - model_flux)

for j, q in enumerate(quarters):
    print("\nQUARTER", q)
    res = 0.5*(res_i[j] + res_q[j])
    x_ = np.arange(len(res))
    
    bad_i = np.abs(res_i[j] - np.mean(res_i[j]))/astropy.stats.mad_std(res_i[j]) > 5.0
    bad_q = np.abs(res_q[j] - np.mean(res_q[j]))/astropy.stats.mad_std(res_q[j]) > 5.0
    
    bad = bad_i * bad_q
    
    print(" outliers rejected:", np.sum(bad))
    print(" marginal outliers:", np.sum(bad_i*~bad_q)+np.sum(~bad_i*bad_q))

bad_lc = []
bad_sc = []

for q in range(18):
    if all_dtype[q] == 'long_no_transits':
        bad = np.ones(np.sum(lc.quarter == q), dtype='bool')
        bad_lc = np.hstack([bad_lc, bad])
        
        
    if all_dtype[q] == 'short_no_transits':
        bad = np.ones(np.sum(sc.quarter == q), dtype='bool')
        bad_sc = np.hstack([bad_sc, bad])    
    
    
    if (all_dtype[q] == 'short') + (all_dtype[q] == 'long'):
        j = np.where(quarters == q)[0][0]

        res = 0.5*(res_i[j] + res_q[j])
        x_ = np.arange(len(res))

        bad_i = np.abs(res_i[j] - np.mean(res_i[j]))/astropy.stats.mad_std(res_i[j]) > 5.0
        bad_q = np.abs(res_q[j] - np.mean(res_q[j]))/astropy.stats.mad_std(res_q[j]) > 5.0

        bad = bad_i * bad_q

        if all_dtype[q] == 'short':
            bad_sc = np.hstack([bad_sc, bad])

        if all_dtype[q] == 'long':
            bad_lc = np.hstack([bad_lc, bad])
        
        
bad_lc = np.array(bad_lc, dtype='bool')
bad_sc = np.array(bad_sc, dtype='bool')


if sc is not None:
    good_cadno_sc = sc.cadno[~bad_sc]
    
if lc is not None:
    good_cadno_lc = lc.cadno[~bad_lc]


######################
# - 2ND DETRENDING - #
######################

print("\nResetting to raw MAST data an performing 2nd DETRENDING...\n")

# reset LONG CADENCE to raw MAST downloads
if lc is not None:
    lc_data = io.cleanup_lkfc(lc_raw_collection, KIC)
    
# make sure there is at least one transit in the long cadence data
# this shouldn't be an issue for real KOIs, but can happen for simulated data
if np.sum(np.array(all_dtype) == 'long') == 0:
    lc_data = []
    
    
lc_quarters = []
for i, lcd in enumerate(lc_data):
    lc_quarters.append(lcd.quarter)
    
    
# reset SHORT CADENCE to raw MAST downloads
if sc is not None:
    sc_data = io.cleanup_lkfc(sc_raw_collection, KIC)

# make sure there is at least one transit in the short cadence data
# this shouldn't be an issue for real KOIs, but can happen for simulated data
if np.sum(np.array(all_dtype) == 'short') == 0:
    sc_data = []
    
    
sc_quarters = []
for i, scd in enumerate(sc_data):
    sc_quarters.append(scd.quarter)

    
# convert LightKurves to LiteCurves
sc_lite = []
lc_lite = []

for i, scd in enumerate(sc_data):
    sc_lite.append(io.LightKurve_to_LiteCurve(scd))
    
for i, lcd in enumerate(lc_data):
    lc_lite.append(io.LightKurve_to_LiteCurve(lcd))
    
    
# removed flagged cadences
sc_data = []
for i, scl in enumerate(sc_lite):
    qmask = np.isin(scl.cadno, good_cadno_sc)
    
    if np.sum(qmask)/len(qmask) > 0.1:
        sc_data.append(scl.remove_flagged_cadences(qmask))

lc_data = []
for i, lcl in enumerate(lc_lite):
    qmask = np.isin(lcl.cadno, good_cadno_lc)
    
    if np.sum(qmask)/len(qmask) > 0.1:
        lc_data.append(lcl.remove_flagged_cadences(qmask))


# detrend long cadence data
break_tolerance = np.max([int(DURS.min()/(LCIT/60/24)*5/2), 13])
min_period = 1.0

for i, lcd in enumerate(lc_data):
    print("QUARTER {}".format(lcd.quarter[0]))
    
    # make transit mask
    lcd.mask = np.zeros(len(lcd.time), dtype='bool')
    for npl, p in enumerate(planets):
        masksize = np.max([1/24, 0.5*p.duration + ttv_buffer[npl]])
        lcd.mask += detrend.make_transitmask(lcd.time, p.tts, masksize)
    
    try:
        lcd = detrend.flatten_with_gp(lcd, break_tolerance, min_period)
    except:
        warnings.warn("Initial detrending model failed...attempting to refit without exponential ramp component")
        try:
            lcd = detrend.flatten_with_gp(lcd, break_tolerance, min_period, correct_ramp=False)
        except:
            warnings.warn("Detrending with RotationTerm failed...attempting to detrend with SHOTerm")
            lcd = detrend.flatten_with_gp(lcd, break_tolerance, min_period, kterm="SHOTerm", correct_ramp=False) 
            
if len(lc_data) > 0:
    lc = detrend.stitch(lc_data)
else:
    lc = None

# detrend short cadence data
break_tolerance = np.max([int(DURS.min()/(SCIT/3600/24)*5/2), 91])
min_period = 1.0

for i, scd in enumerate(sc_data):
    print("QUARTER {}".format(scd.quarter[0]))
    
    # make transit mask
    scd.mask = np.zeros(len(scd.time), dtype='bool')
    for npl, p in enumerate(planets):
        masksize = np.max([1/24, 0.5*p.duration + ttv_buffer[npl]])
        scd.mask += detrend.make_transitmask(scd.time, p.tts, masksize)
    
    try:
        scd = detrend.flatten_with_gp(scd, break_tolerance, min_period)
    except:
        warnings.warn("Initial detrending model failed...attempting to refit without exponential ramp component")
        try:
            scd = detrend.flatten_with_gp(scd, break_tolerance, min_period, correct_ramp=False)
        except:
            warnings.warn("Detrending with RotationTerm failed...attempting to detrend with SHOTerm")
            scd = detrend.flatten_with_gp(scd, break_tolerance, min_period, kterm="SHOTerm", correct_ramp=False)
            
if len(sc_data) > 0:
    sc = detrend.stitch(sc_data)
else:
    sc = None


###########################################
# - MAKE PLOTS, OUTPUT DATA, & CLEAN UP - #
###########################################

# Make individual mask for where each planet transits
# these masks have width 1.5 transit durations, which may be wider than the masks used for detrending

if sc is not None:
    sc_mask = np.zeros((NPL,len(sc.time)),dtype='bool')
    for npl, p in enumerate(planets):
        sc_mask[npl] = detrend.make_transitmask(sc.time, p.tts, np.max([3/24,1.5*p.duration]))
        
    sc.mask = sc_mask.sum(axis=0) > 0

else:
    sc_mask = None

    
if lc is not None:
    lc_mask = np.zeros((NPL,len(lc.time)),dtype='bool')
    for npl, p in enumerate(planets):
        lc_mask[npl] = detrend.make_transitmask(lc.time, p.tts, np.max([3/24,1.5*p.duration]))
        
    lc.mask = lc_mask.sum(axis=0) > 0

else:
    lc_mask = None


# Flag high quality transits (quality = 1)
# good transits must have  at least 50% photometry coverage in/near transit

for npl, p in enumerate(planets):
    count_expect_lc = int(np.ceil(p.duration/lcit))
    count_expect_sc = int(np.ceil(p.duration/scit))
        
    quality = np.zeros(len(p.tts), dtype='bool')
    
    for i, t0 in enumerate(p.tts):
        
        if sc is not None:
            in_sc = np.abs(sc.time - t0)/p.duration < 0.5
            near_sc = np.abs(sc.time - t0)/p.duration < 1.5
            
            qual_in = np.sum(in_sc) > 0.5*count_expect_sc
            qual_near = np.sum(near_sc) > 1.5*count_expect_sc
            
            quality[i] += qual_in*qual_near
        
        
        if lc is not None:
            in_lc = np.abs(lc.time - t0)/p.duration < 0.5
            near_lc = np.abs(lc.time - t0)/p.duration < 1.5
            
            qual_in = np.sum(in_lc) > 0.5*count_expect_lc
            qual_near = np.sum(near_lc) > 1.5*count_expect_lc
            
            quality[i] += qual_in*qual_near
            
    
    p.quality = np.copy(quality)


# Flag which transits overlap (overlap = 1)
overlap = []

for i in range(NPL):
    overlap.append(np.zeros(len(planets[i].tts), dtype='bool'))
    
    for j in range(NPL):
        if i != j:
            for ttj in planets[j].tts:
                overlap[i] += np.abs(planets[i].tts - ttj)/durs.max() < 1.5
                
    planets[i].overlap = np.copy(overlap[i])


# Make phase-folded transit plots
print("\nMaking phase-folded transit plots...\n")

for npl, p in enumerate(planets):
    tts = p.tts[p.quality*~p.overlap]
    
    if len(tts) == 0:
        print("No non-overlapping high quality transits found for planet {0} (P = {1} d)".format(npl, p.period))
    
    else:
        t_folded = []
        f_folded = []

        # grab the data
        for t0 in tts:
            if sc is not None:
                use = np.abs(sc.time-t0)/p.duration < 1.5
                
                if np.sum(use) > 0:
                    t_folded.append(sc.time[use]-t0)
                    f_folded.append(sc.flux[use])
                    
            if lc is not None:
                use = np.abs(lc.time-t0)/p.duration < 1.5
                
                if np.sum(use) > 0:
                    t_folded.append(lc.time[use]-t0)
                    f_folded.append(lc.flux[use])
        
        # sort the data
        t_folded = np.hstack(t_folded)
        f_folded = np.hstack(f_folded)

        order = np.argsort(t_folded)
        t_folded = t_folded[order]
        f_folded = f_folded[order]
        
        # bin the data
        t_binned, f_binned = bin_data(t_folded, f_folded, p.duration/11)
        
        # set undersampling factor and plotting limits
        inds = np.arange(len(t_folded), dtype='int')
        inds = np.random.choice(inds, size=np.min([3000,len(inds)]), replace=False)
        
        ymin = 1 - 3*np.std(f_folded) - p.depth
        ymax = 1 + 3*np.std(f_folded)
        
        # plot the data
        plt.figure(figsize=(12,4))
        plt.plot(t_folded[inds]*24, f_folded[inds], '.', c='lightgrey')
        plt.plot(t_binned*24, f_binned, 'o', ms=8, color='C{0}'.format(npl), label="{0}-{1}".format(TARGET, npl))
        plt.xlim(t_folded.min()*24, t_folded.max()*24)
        plt.ylim(ymin, ymax)
        plt.xticks(fontsize=14)
        plt.yticks(fontsize=14)
        plt.xlabel("Time from mid-transit [hrs]", fontsize=20)
        plt.ylabel("Flux", fontsize=20)
        plt.legend(fontsize=20, loc='upper right', framealpha=1)
        plt.savefig(FIGURE_DIR + TARGET + '_folded_transit_{0:02d}.png'.format(npl), bbox_inches='tight')
        if ~iplot: plt.close()


# Save detrended lightcurves
print("\nSaving detrended lightcurves...\n")

try:
    lc.to_fits(TARGET, DLC_DIR + TARGET + '_lc_detrended.fits')
except:
    print("No long cadence data")

try:
    sc.to_fits(TARGET, DLC_DIR + TARGET + '_sc_detrended.fits')
except:
    print("No short cadence data")


# Exit program
print("")
print("+"*shutil.get_terminal_size().columns)
print("Automated lightcurve detrending complete {0}".format(datetime.now().strftime("%d-%b-%Y at %H:%M:%S")))
print("Total runtime = %.1f min" %((timer()-global_start_time)/60))
print("+"*shutil.get_terminal_size().columns)