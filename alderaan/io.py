from   astropy.io import fits
from   copy import deepcopy
import lightkurve as lk
import numpy as np

from .LiteCurve import LiteCurve


__all__ = ['cleanup_lkfc',
           'LightKurve_to_LiteCurve',
           'load_detrended_lightcurve'
          ]


def cleanup_lkfc(lk_collection, kic):
    """
    Join each quarter in a lk.LightCurveCollection into a single lk.LightCurve
    Performs only the minimal detrending step remove_nans()
    
    Parameters
    ----------
        lk_collection : lk.LightCurveCollection
            lk.LightCurveCollection() with (possibly) multiple entries per quarter
        kic : int
            Kepler Input Catalogue (KIC) number for target
    
    Returns
    -------
        lkc : lk.LightCurveCollection
            lk.LightCurveCollection() with only one entry per quarter
    """
    lk_col = deepcopy(lk_collection)
    
    quarters = []
    for i, lkc in enumerate(lk_col):
        quarters.append(lkc.quarter)

    data_out = []
    for q in np.unique(quarters):
        lkc_list = []
        cadno   = []

        for i, lkc in enumerate(lk_col):
            if (lkc.quarter == q)*(lkc.targetid==kic):
                lkc_list.append(lkc)
                cadno.append(lkc.cadenceno.min())
        
        order = np.argsort(cadno)
        lkc_list = [lkc_list[j] for j in order]

        # the operation "stitch" converts a LightCurveCollection to a single LightCurve
        lkc = lk.LightCurveCollection(lkc_list).stitch().remove_nans()

        data_out.append(lkc)

    return lk.LightCurveCollection(data_out)


def LightKurve_to_LiteCurve(lklc):
    return LiteCurve(time    = np.array(lklc.time.value, dtype='float'),
                     flux    = np.array(lklc.flux.value, dtype='float'),
                     error   = np.array(lklc.flux_err.value, dtype='float'),
                     cadno   = np.array(lklc.cadenceno.value, dtype='int'),
                     quarter = lklc.quarter*np.ones(len(lklc.time), dtype='int'),
                     season  = (lklc.quarter%4)*np.ones(len(lklc.time), dtype='int'),
                     channel = lklc.channel*np.ones(len(lklc.time), dtype='int'),
                     quality = lklc.quality.value
                    )


def load_detrended_lightcurve(filename):
    """
    Load a fits file previously generated by LiteCurve.to_fits()
    
    Parameters
    ----------
        filename : string
    
    Returns
    -------
        litecurve : LiteCurve() object
    
    """     
    litecurve = LiteCurve() 
    
    with fits.open(filename) as hdulist:
        litecurve.time    = np.array(hdulist['TIME'].data, dtype='float64')
        litecurve.flux    = np.array(hdulist['FLUX'].data, dtype='float64')
        litecurve.error   = np.array(hdulist['ERROR'].data, dtype='float64')
        litecurve.cadno   = np.array(hdulist['CADNO'].data, dtype='int')
        litecurve.quarter = np.array(hdulist['QUARTER'].data, dtype='int')
        litecurve.channel = np.array(hdulist['CHANNEL'].data, dtype='int')
        litecurve.mask    = np.asarray(hdulist['MASK'].data, dtype='bool')
        
    return litecurve    