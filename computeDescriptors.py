import errno
import numpy as np
import os
import gc
from enum import Enum, auto
from torch.utils.data import Dataset
from rdkit import Chem
from rdkit.Chem import AllChem, Draw, Descriptors, rdmolfiles, rdMolAlign, rdmolops, rdchem, rdMolDescriptors, ChemicalFeatures
from rdkit.Chem import PeriodicTable, GetPeriodicTable
from rdkit import RDConfig
from rdkit.Chem.FeatMaps import FeatMaps
from rdkit.DataStructs import cDataStructs
from rdkit.Chem.Pharm2D.SigFactory import SigFactory
from rdkit.Chem.Pharm2D import Generate
from rdkit.ML.Descriptors import MoleculeDescriptors
from rdkit.Chem.EState import Fingerprinter
import rdkit.Chem.EState.EState_VSA
import rdkit.Chem.GraphDescriptors
from inspect import getmembers, isfunction, getfullargspec
import h5py
import hashlib


try:
    import cPickle as pickle
except:
    import pickle

#from utils import *
from utils import wiener_index, get_feature_score_vector, mask_borders, ndmesh

from rdkit.Chem import rdRGroupDecomposition as rdRGD
from contextlib import contextmanager,redirect_stderr,redirect_stdout
from os import devnull
@contextmanager
def suppress_stdout_stderr():
    """A context manager that redirects stdout and stderr to devnull"""
    with open(devnull, 'w') as fnull:
        with redirect_stderr(fnull) as err, redirect_stdout(fnull) as out:
            yield (err, out)

class dataBlocks(Enum):
    #def _generate_next_value_(name, start, count, last_values):
        #print(name, start, count, last_values)
        #raise()
        #return count-1
    
    MACCS = 0
    rdkitFP = auto()
    minFeatFP = auto() # causes triangle inequality violations and hence unequal number of features for some entries in the FreeSolv set
    MorganFP = auto()
    
    Descriptors = auto()
    EState_FP = auto()
    Graph_desc = auto()
    
    #extras
    MOE = auto()
    MQN = auto()
    GETAWAY = auto()
    AUTOCORR2D = auto()
    AUTOCORR3D = auto()
    BCUT2D = auto()
    WHIM = auto()
    RDF = auto()
    USR = auto()
    USRCUT = auto()
    PEOE_VSA = auto()
    SMR_VSA = auto()
    SlogP_VSA = auto()
    MORSE = auto()
        
    def __int__(self):
        return self.value
    

    

class CustomMolModularDataset(Dataset):
    def __init__(self, ligs,
                 representation_flags=[1]*(len(dataBlocks)-1), molecular_db_file=None,
                 out_folder=os.path.split(os.path.realpath(__file__))[0], datafolder=os.path.split(os.path.realpath(__file__))[0],
                 normalize_x=False, X_filter=None, verbose=False,
                 cachefolder=None, use_cache=False, use_hdf5_cache=False, use_combined_cache=True,
                 internal_cache_maxMem_MB=512):
        
        self.representation_flags=representation_flags
        self.active_flags=np.where(self.representation_flags)[0]
        self.out_folder=out_folder
        self.datafolder=datafolder
        self.normalize_x=normalize_x
        self.norm_mu=None
        self.norm_width=None
        self.X_filter=None
        self.internal_filtered_cache=None
        self.verbose=verbose
        self.use_cache=use_cache
        self.use_hdf5_cache=use_hdf5_cache
        self.use_combined_cache=use_combined_cache
        
        self._internal_cache_maxMem=internal_cache_maxMem_MB*1024*1024 # 512 MB by default
        
        if(X_filter is not None):
            if type(X_filter) is np.ndarray:
                if(X_filter.ndim!=1):
                    raise ValueError("X_filter should a 1D array or a filename of a pickled 1D array.")
                self.X_filter=X_filter
            elif(not os.path.exists(X_filter)):
                raise(Exception(f"No such file: {X_filter}"))
            else:
                with open(X_filter, 'rb') as f:
                    self.X_filter=pickle.load(f)
        self.ligs=ligs
        if(self.representation_flags[int(dataBlocks.minFeatFP)]):
            fdefName = self.out_folder+'/MinimalFeatures.fdef'
            featFactory = ChemicalFeatures.BuildFeatureFactory(fdefName)
            self.sigFactory = SigFactory(featFactory,minPointCount=2,maxPointCount=3, trianglePruneBins=False)
            self.sigFactory.SetBins([(0,2),(2,5),(5,8)])
            self.sigFactory.Init()
        

            

        #representation cache path precalc
        
        #repr_hash=str(abs(hash(np.array(self.representation_flags, dtype=int).tobytes())))[:5]
        repr_hash=hashlib.md5(np.packbits(np.array(representation_flags, dtype=bool)).tobytes()).hexdigest()
        if(cachefolder is None):
            self.cachefolder=f"{self.datafolder}/combined_modular_repr_cache/{repr_hash}"        
        else:
            self.cachefolder=cachefolder
        if (self.use_combined_cache and not os.path.exists(self.cachefolder)): #make sure the folder exists
            try:
                os.makedirs(self.cachefolder)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                    
        # hdf5 caches
        if(self.use_hdf5_cache):
            if not os.path.exists(self.datafolder+"/modular_repr_cache_hdf5"): #make sure the folder exists
                os.makedirs(self.datafolder+"/modular_repr_cache_hdf5")
            self.hdf5_repr_cache_files=[]
            for i in range(len(self.representation_flags)):
                if(self.representation_flags[i]):
                    fn = self.datafolder+"/modular_repr_cache_hdf5/"+dataBlocks(i).name+".hdf5"
                    self.hdf5_repr_cache_files.append( h5py.File(fn,'a'))
                else:
                    self.hdf5_repr_cache_files.append(None)
                    
    def __del__(self):
        # close hdf5 caches files
        if(self.use_hdf5_cache):
            for i in range(len(self.representation_flags)):
                if(self.representation_flags[i]):
                    self.hdf5_repr_cache_files[i].close()
                    
    def find_ranges(self):
        allX=np.array([entry[0] for entry in self])
        allrange=np.zeros((allX.shape[1],2))
        # allX axis 0 loops over ligands
        allrange[:,0]=np.min(allX, axis=0)
        allrange[:,1]=np.max(allX, axis=0)
        return(allrange)

    def find_normalization_factors(self):
        # read the normalization cache if it was previusly saved
        filt_spec="_no_X_filter"
        fn_no_filt=f"{self.cachefolder}/normalization_factors_{filt_spec}.dat"
        if(self.X_filter is not None):
            filt_hash=hashlib.md5(np.packbits(np.array(self.X_filter, dtype=bool)).tobytes()).hexdigest()
            filt_spec="_fiter_hash_"+filt_hash
        fn=f"{self.cachefolder}/normalization_factors_{filt_spec}.dat"
        if(os.path.exists(fn)):
            temp=np.loadtxt(fn)
            temp=temp.astype(np.float32) # defaults to float64, which translates to torch's double and is incompatible with linear layers
            self.norm_mu=temp[0,:]
            self.norm_width=temp[1,:]
            if(self.verbose):
                print(f"Reading normalization factors for a {norm_mu.shape} dataset")
        elif(os.path.exists(fn_no_filt) and self.X_filter is not None):
            temp=np.loadtxt(fn_no_filt)
            temp=temp.astype(np.float32) # defaults to float64, which translates to torch's double and is incompatible with linear layers
            self.norm_mu=temp[0,self.X_filter]
            self.norm_width=temp[1,self.X_filter]
            if(self.verbose):
                print(f"Reading normalization factors for a {norm_mu.shape} dataset")
        else:
            self.normalize_x=False
            allX=np.array([entry[0] for entry in self])
            self.norm_mu=np.mean(allX, axis=0)
            self.norm_width=np.std(allX, axis=0)
            self.norm_width[self.norm_width<1e-7]=1.0 # if standard deviation is 0, don't scale
            self.normalize_x=True
            
            # save normalization factors
            if not os.path.exists(self.cachefolder): #make sure the folder exists
                os.makedirs(self.cachefolder, exist_ok=True)
            np.savetxt(fn, np.vstack((self.norm_mu, self.norm_width)))
            
            if(self.verbose):
                print(f"Generating normalization factors for a {allX.shape} dataset")
        self.build_internal_filtered_cache()
        _=gc.collect()


    def copy_normalization_factors(self, other):
        if(not np.array_equal(self.X_filter,other.X_filter)):
            raise(Exception("Mismatching X_filters!"))
        self.norm_mu=other.norm_mu
        self.norm_width=other.norm_width
        
    def build_internal_filtered_cache(self):
        if(self.norm_mu is None and self.normalize_x):
            raise(Exception("call build_internal_filtered_cache() only after normalization!"))
        neededMem=len(self)*(self[0][0].shape[0]+self[0][1].shape[0])*self[0][1].itemsize
        if(neededMem>self._internal_cache_maxMem):
            print(f"Building the internal_filtered_cache needs {neededMem/1024/1024} MB, more than the {self._internal_cache_maxMem/1024/1024} MB limit. SKIPPING and will read samples from HDD each time instead.")
            return()
        allX=[]
        allY=[]
        for entry in self: # loop over self only once
            allX.append(entry[0])
            allY.append(entry[1])
        allX=np.array(allX)
        allY=np.array(allY)
        self.internal_filtered_cache=(allX, allY)
        if(self.verbose):
            print(f"saving an internal filtered & normalized cache of shape ({self.internal_filtered_cache[0].shape},{self.internal_filtered_cache[1].shape})")


    def normalize_input(self,x):
        if(self.norm_mu is None):
            self.find_normalization_factors()
        return((x-self.norm_mu)/self.norm_width)



    def __len__(self):
        return len(self.ligs)

    def __getitem__(self, idx):
        lig = self.ligs[idx]

        if(self.internal_filtered_cache is None):
            lig_ID = lig.GetProp("ID")
            #check combined repr cache
            cache_fn = self.cachefolder+'/'+lig_ID+'.pickle'
            if(self.use_combined_cache and os.path.isfile(cache_fn)):
                with open(cache_fn, 'rb') as f:
                    X, Y = pickle.load(f)
            else:
                X = self.transform(idx).astype(np.float32)
                Y = np.array([float(lig.GetProp('dG')) if lig.HasProp('dG') else np.nan]) # kcal/mol
                #save cache
                if(self.use_combined_cache):
                    with open(cache_fn, 'wb') as f:
                        pickle.dump((X, Y), f)
            #if(self.X_filter):
            if(not self.X_filter is None):
                X=X[self.X_filter]
            if(self.normalize_x):
                #print(f"{lig_ID} width: {X.shape}")
                X=self.normalize_input(X)
                
        else:
            X=self.internal_filtered_cache[0][idx]
            Y=self.internal_filtered_cache[1][idx]
        
        return X, Y
            
    def generate_DataBlock(self, lig, blockID):
        blockID=dataBlocks(blockID)
        
        if(blockID==dataBlocks.MACCS):
            Chem.GetSymmSSSR(lig)
            MACCS_txt=cDataStructs.BitVectToText(rdMolDescriptors.GetMACCSKeysFingerprint(lig))
            MACCS_arr=np.zeros(len(MACCS_txt), dtype=np.uint8)
            for j in range(len(MACCS_txt)):
                if(MACCS_txt[j]=="1"):
                    MACCS_arr[j]=1;
            return(MACCS_arr)
        
        elif(blockID==dataBlocks.MorganFP):
            Chem.GetSymmSSSR(lig)
            Morgan_txt=cDataStructs.BitVectToText(rdMolDescriptors.GetMorganFingerprintAsBitVect(lig, 2))
            Morgan_arr=np.zeros(len(Morgan_txt), dtype=np.uint8)
            for j in range(len(Morgan_txt)):
                if(Morgan_txt[j]=="1"):
                    Morgan_arr[j]=1;
            return(Morgan_arr)
        
        elif(blockID==dataBlocks.rdkitFP):
            Chem.GetSymmSSSR(lig)
            rdkitFingerprint_txt=cDataStructs.BitVectToText(Chem.rdmolops.RDKFingerprint(lig))
            rdkitFingerprint_arr=np.zeros(len(rdkitFingerprint_txt), dtype=np.uint8)
            for j in range(len(rdkitFingerprint_txt)):
                if(rdkitFingerprint_txt[j]=="1"):
                    rdkitFingerprint_arr[j]=1;
            return(rdkitFingerprint_arr)
        
        elif(blockID==dataBlocks.minFeatFP):
           Chem.GetSymmSSSR(lig)
           minFeatFingerprint_txt=cDataStructs.BitVectToText(Generate.Gen2DFingerprint(lig, self.sigFactory))
           minFeatFingerprint_arr=np.zeros(len(minFeatFingerprint_txt), dtype=np.uint8)
           for j in range(len(minFeatFingerprint_txt)):
               if(minFeatFingerprint_txt[j]=="1"):
                   minFeatFingerprint_arr[j]=1;
           return(minFeatFingerprint_arr)
    
        elif(blockID==dataBlocks.Descriptors):
            nms=[x[0] for x in Descriptors._descList]
            calc = MoleculeDescriptors.MolecularDescriptorCalculator(nms)
            des = np.array(calc.CalcDescriptors(lig))
            return(des)
        
        elif(blockID==dataBlocks.EState_FP):
            ES=Fingerprinter.FingerprintMol(lig)
            funcs=getmembers(rdkit.Chem.GraphDescriptors, isfunction)
            funcs=[f[1] for f in funcs if f[0][0]!='_' and len(getfullargspec(f[1])[0])==1]
            ES_VSA=np.array([f(lig) for f in funcs])
            ES_FP=np.concatenate((ES[0],ES[1],ES_VSA))
            return(ES_FP)
    
        elif(blockID==dataBlocks.Graph_desc):
            funcs=getmembers(rdkit.Chem.GraphDescriptors, isfunction)
            funcs=[f[1] for f in funcs if f[0][0]!='_' and len(getfullargspec(f[1])[0])==1]
            funcs+=[wiener_index]
            graph_desc=np.array([f(lig) for f in funcs])
            return(graph_desc)
    
        
        #extras
        elif(blockID==dataBlocks.MOE):
            funcs=getmembers(rdkit.Chem.MolSurf, isfunction)
            funcs=[f[1] for f in funcs if f[0][0]!='_' and len(getfullargspec(f[1])[0])==1]
            MOE=np.array([f(lig) for f in funcs])
            return(MOE)
        elif(blockID==dataBlocks.MQN):
            return(np.array(rdMolDescriptors.MQNs_(lig) ))
        elif(blockID==dataBlocks.GETAWAY):
            return(np.array(rdMolDescriptors.CalcGETAWAY(lig) ))
        elif(blockID==dataBlocks.AUTOCORR2D):
            return(np.array(rdMolDescriptors.CalcAUTOCORR2D(lig) ))
        elif(blockID==dataBlocks.AUTOCORR3D):
            return(np.array(rdMolDescriptors.CalcAUTOCORR3D(lig) ))
        elif(blockID==dataBlocks.BCUT2D):
            return(np.array(rdMolDescriptors.BCUT2D(lig) ))
        elif(blockID==dataBlocks.WHIM):
            return(np.array(rdMolDescriptors.CalcWHIM(lig) ))
        elif(blockID==dataBlocks.RDF):
            return(np.array(rdMolDescriptors.CalcRDF(lig) ))
        elif(blockID==dataBlocks.USR):
            return(np.array(rdMolDescriptors.GetUSR(lig) ))
        elif(blockID==dataBlocks.USRCUT):
            return(np.array(rdMolDescriptors.GetUSRCAT(lig) ))
        elif(blockID==dataBlocks.PEOE_VSA):
            return(np.array(rdMolDescriptors.PEOE_VSA_(lig) ))
        elif(blockID==dataBlocks.SMR_VSA):
            return(np.array(rdMolDescriptors.SMR_VSA_(lig) ))
        elif(blockID==dataBlocks.SlogP_VSA):
            return(np.array(rdMolDescriptors.SlogP_VSA_(lig) ))
        elif(blockID==dataBlocks.MORSE):
            return(np.array(rdMolDescriptors.CalcMORSE(lig) ))
            
        else:
            raise(Exception(f"Unsupported dataBlock requested: {blockID}"))
        
        
            

    def transform(self, lig_idx):
        vecs=[]
        #for i in range(len(self.representation_flags)):
        #    if(self.representation_flags[i]):
                
        for i in self.active_flags:
            #where are the block chaches?
            cache_folder=self.datafolder+"/modular_repr_cache/"+dataBlocks(i).name+"/"
                            
            if not os.path.exists(cache_folder): #make sure the folder exists
                try:
                    os.makedirs(cache_folder)
                except OSError as e:
                    if e.errno != errno.EEXIST:
                        raise
                    
            #if block is cached, read it
            lig_ID = self.ligs[lig_idx].GetProp("ID")
            cache_fn = cache_folder+'/'+lig_ID+'.pickle'
            hdf5_tn=f"/{dataBlocks(i).name}/{lig_ID}"
            # if(self.use_hdf5_cache and hdf5_tn in self.hdf5_repr_cache_files[i]): #try hdf5 cache first
            #     X_block_rep=self.hdf5_repr_cache_files[i][hdf5_tn][:]
            # elif(os.path.isfile(cache_fn)): #try pickle cache second
            #     with open(cache_fn, 'rb') as f:
            #         X_block_rep = pickle.load(f)
            #     if(self.use_hdf5_cache): # also make the hdf5 cache from pickles
            #         self.hdf5_repr_cache_files[i].create_dataset(hdf5_tn, data=X_block_rep, dtype='f')
            # else: #generate a block and cache it otherwize
            X_block_rep = self.generate_DataBlock(self.ligs[lig_idx], i)
                    
                # if(self.use_cache):
                #     with open(cache_fn, 'wb') as f:
                #         pickle.dump(X_block_rep, f)
                # if(self.use_hdf5_cache):
                #     self.hdf5_repr_cache_files[i].create_dataset(hdf5_tn, data=X_block_rep, dtype='f')
            
            #add to overall representation
            vecs.append(X_block_rep)
        return(np.concatenate(tuple(vecs), axis=0))




