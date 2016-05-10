"""
This file defines the class BoostedParticleDiagnostic

Major features:
- The class reuses the existing methods of ParticleDiagnostic
  as much as possible, through class inheritance
- The class implements memory buffering of the slices, so as
  not to write to disk at every timestep
"""
import os
import numpy as np
from scipy.constants import c
from .particle_diag import ParticleDiagnostic

# If numbapro is installed, it potentially allows to use a GPU
try :
    from fbpic.cuda_utils import cuda, cuda_tpb_bpg_1d
    cuda_installed = cuda.is_available()
except ImportError:
    cuda_installed = False

class BoostedParticleDiagnostic(ParticleDiagnostic):
    """
    Class that writes the particles *in the lab frame*, 
    from a simulation in the boosted frame
    
    Particles are extracted from the simulation in slices each time step
    and buffered in memory before writing to disk. On the CPU, slices of 
    particles are directly selected from the particle arrays of the species.
    On the GPU, first particles within an area of cells surrounding the 
    output planes are extracted from the GPU particle arrays and stored in 
    a smaller GPU array, which is then copied to the CPU for selection.
    The mechanism of extracting the particles within the outputplane-area
    on the GPU relies on particle arrays being sorted on the GPU. For the
    back-transformation to the Lab frame, interpolation in space is applied,
    but no interpolation for the particle velocities is applied.
    
    Usage
    -----
    After initialization, the diagnostic is called by using 
    the 'write' method.
    """
    def __init__(self, zmin_lab, zmax_lab, v_lab, dt_snapshots_lab,
                 Ntot_snapshots_lab, gamma_boost, period, fldobject, 
                 particle_data=["position", "momentum", "weighting"],
                 select=None, write_dir=None, species={"electrons": None},
                 comm = None):
        """
        Initialize diagnostics that retrieve the data in the lab frame,
        as a series of snapshot (one file per snapshot),
        within a virtual moving window defined by zmin_lab, zmax_lab, v_lab.
                     
        Parameters
        ----------
        zmin_lab, zmax_lab: floats (meters)
            Positions of the minimum and maximum of the virtual moving window,
            *in the lab frame*, at t=0

        v_lab: float (m.s^-1)
            Speed of the moving window *in the lab frame*

        dt_snapshots_lab: float (seconds)
            Time interval *in the lab frame* between two successive snapshots

        Ntot_snapshots_lab: int
            Total number of snapshots that this diagnostic will produce

        period: int
            Number of iterations for which the data is accumulated in memory,
            before finally writing it to the disk.

        fldobject : a Fields object,
            The Fields object of the simulation, that is needed to
            extract some information about the grid
                
        See the documentation of ParticleDiagnostic for the other parameters

        """
        # Do not leave write_dir as None, as this may conflict with
        # the default directory ('./diags') in which diagnostics in the
        # boosted frame are written
        if write_dir is None:
            write_dir = 'lab_diags'
        
        # Initialize Particle diagnostic normal attributes
        ParticleDiagnostic.__init__(self, period, species, 
            comm, particle_data, select, write_dir)

        # Register the Field object
        self.fld = fldobject

        # Register the boost quantities
        self.gamma_boost = gamma_boost
        self.inv_gamma_boost = 1./gamma_boost
        self.beta_boost = np.sqrt(1. - self.inv_gamma_boost**2)
        self.inv_beta_boost = 1./self.beta_boost

        # Create the list of LabSnapshot objects
        self.snapshots = []
        for i in range( Ntot_snapshots_lab ):
            t_lab = i*dt_snapshots_lab
            snapshot = LabSnapshot( t_lab,
                                    zmin_lab + v_lab*t_lab,
                                    zmax_lab + v_lab*t_lab,
                                    self.dt,
                                    self.write_dir, i ,self.species_dict )
            self.snapshots.append(snapshot)
            # Initialize a corresponding empty file to store particles
            self.create_file_empty_slice(
                    snapshot.filename, i, snapshot.t_lab, self.dt)

        # Create the ParticleCatcher object
        # (This object will extract the particles (slices) that crossed the 
        # output plane at each iteration.)
        self.particle_catcher = ParticleCatcher(
            self.gamma_boost, self.beta_boost, self.fld )

    def write( self, iteration ): 
        """
        Redefines the method write of the parent class ParticleDiagnostic

        Parameters
        ----------
        iteration : int
            Current iteration of the boosted frame simulation
        """
        # At each timestep, store a slice of the particles in memory buffers 
        self.store_snapshot_slices(iteration)
        
        # Every self.period, write the buffered slices to disk 
        if iteration % self.period == 0:
            self.flush_to_disk()
        
    def store_snapshot_slices( self, iteration ):
        """
        Store slices of the particles in the memory buffers of the
        corresponding lab snapshots

        Parameters
        ----------
        iteration : int
            Current iteration of the boosted frame simulation 
        """
        # Find the limits of the local subdomain at this iteration
        zmin_boost = self.fld.interp[0].zmin
        zmax_boost = self.fld.interp[0].zmax

        # Extract the current time in the boosted frame
        time = iteration * self.dt

        # Loop through the labsnapshots
        for snapshot in self.snapshots:

            # Update the positions of the output slice of this snapshot
            # in the lab and boosted frame (current_z_lab and current_z_boost)
            snapshot.update_current_output_positions( time,
                self.inv_gamma_boost, self.inv_beta_boost)

            # For this snapshot:
            # - check if the output position *in the boosted frame*
            #   is in the current local domain
            # - check if the output position *in the lab frame*
            #   is within the lab-frame boundaries of the current snapshot
            if ( (snapshot.current_z_boost > zmin_boost) and \
                 (snapshot.current_z_boost < zmax_boost) and \
                 (snapshot.current_z_lab > snapshot.zmin_lab) and \
                 (snapshot.current_z_lab < snapshot.zmax_lab) ):
                
                # Loop through the particle species and register the
                # particle arrays in the snapshot objects (buffering)
                for species_name, species in self.species_dict.iteritems():
                    # Extract the slice of particles
                    slice_array = self.particle_catcher.extract_slice( 
                        species, snapshot.current_z_boost, 
                        snapshot.prev_z_boost, time, self.select)
                    # Register new slice in the LabSnapshot
                    snapshot.register_slice( slice_array, species_name )

    def flush_to_disk(self):
        """
        Writes the buffered slices of particles to the disk. Erase the 
        buffered slices of the LabSnapshot objects
        """
        # Loop through the labsnapshots and flush the data
        for snapshot in self.snapshots:
            
            # Compact the successive slices that have been buffered
            # over time into a single array
            for species_name in self.species_dict:
                
                if snapshot.buffered_slices[species_name] != []:
                    # Compact the slices in a single array
                    particle_array = snapshot.compact_slices(species_name)
                
                    # Write this array to disk
                    # (if this snapshot has new slices)
                    if particle_array.size:
                        self.write_slices( particle_array, species_name, 
                            snapshot, self.particle_catcher.particle_to_index )

                snapshot.buffered_slices[species_name] = []

    def write_slices( self, particle_array, species_name, snapshot, p2i ): 
        """
        For one given snapshot, write the slices of the
        different species to an openPMD file

        Parameters
        ----------
        particle_array: array of reals
            Array of shape (7, num_part) 

        species_name: String
            A String that acts as the key for the buffered_slices dictionary

        snapshot: a LabSnaphot object

        p2i: dict
            Dictionary of correspondance between the particle quantities
            and the integer index in the particle_array
        """
        # Open the file without parallel I/O in this implementation
        f = self.open_file(snapshot.filename)
        particle_path = "/data/%d/particles/%s" %(snapshot.iteration, 
            species_name)
        species_grp = f[particle_path]

        # Loop over the different quantities that should be written
        for particle_var in self.particle_data:

            if particle_var == "position":
                for coord in ["x","y","z"]:
                    quantity= coord
                    path = "%s/%s" %(particle_var, quantity)
                    data = particle_array[ p2i[ quantity ] ]
                    self.write_particle_slices(species_grp, path, data, 
                        quantity)
     
            elif particle_var == "momentum":
                for coord in ["x","y","z"]:
                    quantity= "u%s" %coord
                    path = "%s/%s" %(particle_var,coord)
                    data = particle_array[ p2i[ quantity ] ]
                    self.write_particle_slices( species_grp, path, data,
                        quantity)
                
            elif particle_var == "weighting":
               quantity= "w"
               path = 'weighting'
               data = particle_array[ p2i[ quantity ] ]
               self.write_particle_slices(species_grp, path, data,
                    quantity)
            
        # Close the file
        f.close()

    def write_particle_slices( self, species_grp, path, data, quantity ):
        """
        Writes each quantity of the buffered dataset to the disk, the 
        final step of the writing
        """
        dset = species_grp[path]
        index = dset.shape[0]

        # Resize the h5py dataset 
        dset.resize(index+len(data), axis=0)

        # Write the data to the dataset at correct indices
        dset[index:] = data

    def create_file_empty_slice( self, fullpath, iteration, time, dt ):
        """
        Create an openPMD file with empty meshes and setup all its attributes

        Parameters
        ----------
        fullpath: string
            The absolute path to the file to be created

        iteration: int
            The iteration number of this diagnostic

        time: float (seconds)
            The physical time at this ibteration

        dt: float (seconds)
            The timestep of the simulation
        """
        # Create the file
        f = self.open_file( fullpath )

        # Setup the different layers of the openPMD file
        # (f is None if this processor does not participate is writing data)
        if f is not None:

            # Setup the attributes of the top level of the file
            self.setup_openpmd_file( f, iteration, time, dt )
            # Setup the meshes group (contains all the particles)
            particle_path = "/data/%d/particles/" %iteration

            for species_name, species in self.species_dict.iteritems():
                species_path = particle_path+"%s/" %(species_name)
                # Create and setup the h5py.Group species_grp
                species_grp = f.require_group( species_path )
                self.setup_openpmd_species_group( species_grp, species )
                
                # Loop over the different quantities that should be written
                # and setup the corresponding datasets
                for particle_var in self.particle_data :

                    if particle_var == "position" :
                        for coord in ["x", "y", "z"] :
                            quantity = coord
                            quantity_path = "%s/%s" %(particle_var, coord)
                            dset = species_grp.require_dataset(
                                quantity_path, (0,), 
                                maxshape=(None,), dtype='f')        
                            self.setup_openpmd_species_component( 
                                dset, quantity )
                            self.setup_openpmd_species_record( 
                                species_grp[particle_var], particle_var )
                        
                    elif particle_var == "momentum" :
                        for coord in ["x", "y", "z"] :
                            quantity = "u%s" %(coord)
                            quantity_path = "%s/%s" %(particle_var, coord)
                            dset = species_grp.require_dataset(
                                quantity_path, (0,), 
                                maxshape=(None,), dtype='f')        
                            self.setup_openpmd_species_component( 
                                dset, quantity )
                            self.setup_openpmd_species_record( 
                                species_grp[particle_var], particle_var )
                                
                    elif particle_var == "weighting" :
                        quantity = "w"
                        quantity_path = "weighting"
                        dset = species_grp.require_dataset(
                            quantity_path, (0,), 
                            maxshape=(None,), dtype='f')        
                        self.setup_openpmd_species_component( 
                            dset, quantity )
                        self.setup_openpmd_species_record( 
                            species_grp[particle_var], particle_var )
                        
                    else :
                        raise ValueError("Invalid string in %s of species" 
                                             %(particle_var))

            # Close the file
            f.close()

class LabSnapshot:
    """
    Class that stores data relative to one given snapshot
    in the lab frame (i.e. one given *time* in the lab frame)
    """
    def __init__( self, t_lab, zmin_lab, zmax_lab, dt, 
                  write_dir, i, species_dict ):
        """
        Initialize a LabSnapshot 

        Parameters
        ----------
        t_lab: float (seconds)
            Time of this snapshot *in the lab frame*
            
        zmin_lab, zmax_lab: floats (meters)
            Longitudinal limits of this snapshot

        write_dir: string
            Absolute path to the directory where the data for
            this snapshot is to be written

        dt : float (s)
            The timestep of the simulation in the boosted frame

        i: int
            Number of the file where this snapshot is to be written

        species_dict: dict
            Contains all the species name of the species object 
            (inherited from Warp)
        """
        # Deduce the name of the filename where this snapshot writes
        self.filename = os.path.join( write_dir, 'hdf5/data%08d.h5' %i)
        self.iteration = i
        self.dt = dt

        # Time and boundaries in the lab frame (constant quantities)
        self.zmin_lab = zmin_lab
        self.zmax_lab = zmax_lab
        self.t_lab = t_lab

        # Positions where the fields are to be registered
        # (Change at every iteration)
        self.current_z_lab = 0
        self.current_z_boost = 0

        # Initialize empty dictionary to buffer the slices for each species
        self.buffered_slices = {}
        for species in species_dict:
            self.buffered_slices[species] = []

    def update_current_output_positions( self, t_boost, inv_gamma, inv_beta ):
        """
        Update the current and previous positions of output for this snapshot,
        so that it corresponds to the time t_boost in the boosted frame

        Parameters
        ----------
        t_boost: float (seconds)
            Time of the current iteration, in the boosted frame

        inv_gamma, inv_beta: floats
            Inverse of the Lorentz factor of the boost, and inverse
            of the corresponding beta
        """
        # Some shorcuts for further calculation's purposes
        t_lab = self.t_lab  
        t_boost_prev = t_boost - self.dt

        # This implements the Lorentz transformation formulas,
        # for a snapshot having a fixed t_lab
        self.current_z_boost = (t_lab*inv_gamma - t_boost)*c*inv_beta
        self.prev_z_boost = (t_lab*inv_gamma - t_boost_prev)*c*inv_beta     
        self.current_z_lab = (t_lab - t_boost*inv_gamma)*c*inv_beta
        self.prev_z_lab = (t_lab - t_boost_prev*inv_gamma)*c*inv_beta
    
    def register_slice( self, slice_array, species ):
        """
        Store the slice of particles represented by slice_array

        Parameters
        ----------
        slice_array: array of reals
            An array of packed fields that corresponds to one slice,
            as given by the ParticleCatcher object

        species: String, key of the species_dict
            Act as the key for the buffered_slices dictionary
        """
        # Store the values 
        self.buffered_slices[species].append(slice_array)

    def compact_slices( self, species ):
        """
        Compact the successive slices that have been buffered
        over time into a single array.

        Parameters
        ----------
        species: String, key of the species_dict
            Act as the key for the buffered_slices dictionary

        Returns
        -------
        paticle_array: an array of reals of shape (7, numPart) 
        regardless of the dimension

        Returns None if the slices are empty
        """
        particle_array = np.concatenate(
            self.buffered_slices[species], axis=1)

        return particle_array

class ParticleCatcher:
    """
    Class that extracts, Lorentz-transforms and gathers particles
    """
    def __init__( self, gamma_boost, beta_boost, fldobject ):
        """
        Initialize the ParticleCatcher object

        Parameters
        ----------
        gamma_boost, beta_boost: float
            The Lorentz factor of the boost and the corresponding beta

        fldobject : a Fields object,
            The Fields object of the simulation, that is needed to
            extract some information about the grid
        """
        # Some attributes necessary for particle selections
        self.gamma_boost = gamma_boost
        self.beta_boost = beta_boost

        # Register the fields object
        self.fld = fldobject
        self.dt = self.fld.dt
        
        # Create a dictionary that contains the correspondance
        # between the particles quantity and array index
        self.particle_to_index = {'x':0, 'y':1, 'z':2, 
                                  'ux':3,'uy':4, 'uz':5,
                                  'inv_gamma':6, 'w':7}

    def extract_slice( self, species, current_z_boost, previous_z_boost, 
                       t, select=None ):
        """
        Extract a slice of the particles at z_boost and if select is present,
        extract only the particles that satisfy the given criteria 

        Parameters
        ----------
        species : A ParticleObject
            Contains the particle attributes to output

        current_z_boost, previous_z_boost : float (m)
            Current and previous position of the output plane
            in the boosted frame

        t : float (s)
            Current time of the simulation in the boosted frame

        select : dict 
            A set of rules defined by the users in selecting the particles
            z: {"uz" : [50, 100]} for particles which have normalized 
            momenta between 50 and 100

        Returns
        -------
        slice_array : An array of reals of shape (7, numPart) 
            An array that packs together the slices of the different 
            particles.
        """
        # Get a dictionary containing the particle data
        # When running on the GPU, this only copies to CPU the particles 
        # within a small area around the output plane.
        particle_data = self.get_particle_data( species, 
                            current_z_boost, previous_z_boost, t )

        # Get the selection of particles (slice) that crossed the 
        # output plane during the last iteration
        slice_array = self.get_particle_slice( 
                particle_data, current_z_boost, previous_z_boost )

        # Backpropagate particles to correct output position and 
        # transform particle attributes to the lab frame
        slice_array = self.interpolate_particles_to_lab_frame( 
            slice_array, current_z_boost, t )

        # Choose the particles based on the select criteria defined by the 
        # users. Notice: this implementation still comes with a cost, 
        # one way to optimize it would be to do the selection before Lorentz
        # transformation back to the lab frame
        if (select is not None) and slice_array.size:
            # Find the particles that should be selected
            select_array = self.apply_selection(select, slice_array)
            # Keep only those particles in slice_array
            slice_array = slice_array[:, select_array]

        # Convert data to the OpenPMD standard
        slice_array = self.apply_opmd_standard( slice_array, species )

        return slice_array

    def get_particle_data( self, species, current_z_boost, 
                           previous_z_boost, t ):
        """
        Extract the particle data from the species object.
        In case CUDA is used, only a selection of particles
        (i.e. particles that are within cells corresponding
        to the immediate neighborhood of the output plane)
        is received from the GPU (increases performance).

        Parameters
        ----------
        species : A ParticleObject
            Contains the particle attributes to output

        current_z_boost, previous_z_boost : float (m)
            Current and previous position of the output plane
            in the boosted frame

        t : float (s)
            Current time of the simulation in the boosted frame

        Returns
        -------
        particle_data : A dictionary of 1D float arrays
            A dictionary that contains the particle data of 
            the simulation (with normalized weigths).
        """
        # CPU
        if species.use_cuda is False:
            # Create a dictionary containing the particle attributes
            particle_data = {
                'x' : species.x, 'y' : species.y, 'z' : species.z,
                'ux' : species.ux, 'uy' : species.uy, 'uz' : species.uz,
                'w' : species.w, 'inv_gamma' : species.inv_gamma }
        # GPU
        else:
            # Check if particles are sorted, otherwise raise exception
            if species.sorted == False:
                raise ValueError('Particle boosted-frame diagnostic: \
                 The particles are not sorted!')
            # Precalculating quantities and shortcuts
            dt = self.fld.dt
            dz = self.fld.interp[0].dz
            zmin = self.fld.interp[0].zmin
            pref_sum = species.prefix_sum
            Nz, Nr = species.grid_shape
            # Calculate cell area to get particles from 
            # (indices of the current and previous cell representing the
            # boundaries of this area
            cell_curr = int((current_z_boost - zmin - 0.5*dz)/dz)
            cell_prev = int((previous_z_boost - zmin - 0.5*dz + dt*c)/dz)+1
            # Get the prefix sum values for calculation 
            # of number of particles
            pref_sum_curr = pref_sum.getitem( 
                np.fmax( cell_curr*Nr-1, 0 ) ) 
            pref_sum_prev = pref_sum.getitem( 
                np.fmin( cell_prev*Nr-1, Nz*Nr-1) )
            # Calculate number of particles in this area (N_area)
            N_area = pref_sum_prev - pref_sum_curr
            # Check if there are particles to extract
            if N_area > 0:
                # Create empty GPU array for particles
                particle_selection = cuda.device_array( 
                    (8, N_area), dtype=np.float64 )
                # Call kernel that extracts particles from GPU
                dim_grid_1d, dim_block_1d = cuda_tpb_bpg_1d(N_area)
                extract_particles_from_gpu[dim_grid_1d, dim_block_1d](
                     species.x, species.y, species.z,
                     species.ux, species.uy, species.uz,
                     species.w, species.inv_gamma, 
                     particle_selection, pref_sum_curr)
                # Copy GPU array to the host
                part_data = particle_selection.copy_to_host()
            else:
                # Create an empty array if N_area is zero.
                part_data = np.zeros((8,0), np.float64)
            # Create a dictionary containing the particle attributes
            particle_data = {
                'x' : part_data[0], 'y' : part_data[1], 'z' : part_data[2],
                'ux' : part_data[3], 'uy' : part_data[4], 'uz' : part_data[5],
                'w' : part_data[6], 'inv_gamma' : part_data[7]}

        return particle_data 

    def get_particle_slice( self, particle_data, current_z_boost, 
                             previous_z_boost ):
        """
        Get the selection of particles that crossed the output
        plane during the last iteration.

        Parameters
        ----------
        particle_data : float
            Dictionary containing the particle data

        current_z_boost, previous_z_boost : float (m)
            Current and previous position of the output plane
            in the boosted frame

        Returns
        -------
        slice_array : An array of reals of shape (7, numPart) 
            An array that packs together the slices of the different 
            particles.
        """
        # Shortcut
        p2i = self.particle_to_index
        pd = particle_data

        # Calculate current and previous position in z
        current_z = pd['z']
        previous_z = pd['z']-pd['uz']*pd['inv_gamma']*c*self.dt

        # A particle array for mapping purposes
        particle_indices = np.arange(len(current_z))
            
        # For this snapshot:
        # - check if the output position *in the boosted frame*
        #   crosses the zboost in a forward motion
        # - check if the output position *in the boosted frame*
        #   crosses the zboost_prev in a backward motion
        selected_indices = np.compress((((current_z >= current_z_boost) &
            (previous_z <= previous_z_boost)) |
            ((current_z <= current_z_boost) & 
            (previous_z >= previous_z_boost))), particle_indices)

        # Get number of selected particles
        num_part = np.shape(selected_indices)[0]

        # Create empty 2D slice array (7, num_part)
        slice_array = np.empty((np.shape(p2i.keys())[0], num_part,))

        for quantity in p2i.keys():
                # Store particle quantities in a 2D array
                slice_array[ p2i[quantity], ... ] = \
                np.take(particle_data[quantity], selected_indices)

        return slice_array

    def interpolate_particles_to_lab_frame( self, slice_array, current_z_boost, t ):
        """
        Transform the particle quantities from the boosted frame to the
        lab frame. These are classical Lorentz transformation equations

        Parameters
        ----------
        slice_array : float, (7, n_part)
            2D array containing the slices of all quantities

        current_z_boost : float (m)
            Current position of the output plane in the boosted frame

        t : float (s)
            Current time of the simulation in the boosted frame

        Returns
        -------
        slice_array : An array of reals of shape (7, numPart) 
            An array that packs together the slices of the different 
            particles.
        """
        # Shortcut
        p2i = self.particle_to_index

        # Shortcuts for particle attributes
        x = slice_array[p2i['x']]
        y = slice_array[p2i['y']]
        z = slice_array[p2i['z']]
        ux = slice_array[p2i['ux']]
        uy = slice_array[p2i['uy']]
        uz = slice_array[p2i['uz']]
        inv_gamma = slice_array[p2i['inv_gamma']]

        # Calculate time (t_cross) when particle and plane intersect
        # Velocity of the particles
        v_z = uz*inv_gamma*c
        # Velocity of the plane
        v_plane = -c/self.beta_boost
        # Time in the boosted frame when particles cross the output plane
        t_cross = t - (current_z_boost - z) / (v_plane - v_z)

        # Push particles to position of plane intersection
        x += c*(t_cross - t)*inv_gamma*ux
        y += c*(t_cross - t)*inv_gamma*uy
        z += c*(t_cross - t)*inv_gamma*uz

        # Back-transformation of position with updated time (t_cross)
        z_lab = self.gamma_boost*( z + self.beta_boost*c*t_cross )
 
        # Back-transformation of momentum
        gamma = np.sqrt(1. + (ux**2 + uy**2 + uz**2))
        uz_lab = self.gamma_boost*uz \
            + gamma*(self.beta_boost*self.gamma_boost)

        # Write the modified quantities to slice_array
        slice_array[p2i['x'],:] = x
        slice_array[p2i['y'],:] = y
        slice_array[p2i['z'],:] = z_lab
        slice_array[p2i['ux'],:] = ux
        slice_array[p2i['uy'],:] = uy
        slice_array[p2i['uz'],:] = uz_lab

        return slice_array

    def apply_opmd_standard( self, slice_array, species ):
        """
        Apply the OpenPMD standard to the particle quantities.
        Momentum (u) is multiplied by m * c and weights are 
        divided by the particle charge q.

        Parameters
        ----------
        slice_array : 2D array of floats
            Contains the particle slice data to output.

        species : A ParticleObject
            Contains the particle data and the meta-data
            needed for the conversion to the OpenPMD format.

        Returns
        -------
        slice_array : 2D array of floats
            Contains the particle slice data to output in 
            the OpenPMD format.
        """
        # Normalize momenta
        for quantity in ['ux', 'uy', 'uz']:
            idx = self.particle_to_index[quantity]
            slice_array[idx] *= species.m * c
        # Normalize weights
        idx = self.particle_to_index['w']
        slice_array[idx] *= 1./species.q

        return slice_array

    def apply_selection( self, select, slice_array ) :
        """
        Apply the rules of self.select to determine which
        particles should be written

        Parameters
        ----------
        select : a dictionary that defines all selection rules based
        on the quantities

        slice_array: 2d array of floats
           An array of shape (7, num_part) which contains the particle slice 
           data, from which particle data is to be further selected 
           according to `select`

        Returns
        -------
        select_array: 1darray of bools
            A 1darray of shape (num_part,) containing True for the particles 
            that satisfy all the rules of select.
        """
        p2i = self.particle_to_index

        # Initialize an array filled with True
        select_array = np.ones( np.shape(slice_array)[1], dtype='bool' )

        # Apply the rules successively
        # Go through the quantities on which a rule applies
        for quantity in select.keys() :
            # Lower bound
            if select[quantity][0] is not None :
                select_array = np.logical_and(
                    slice_array[p2i[quantity]] >\
                     select[quantity][0], select_array )
            # Upper bound
            if select[quantity][1] is not None :
                select_array = np.logical_and(
                    slice_array[p2i[quantity]] <\
                    select[quantity][1], select_array )

        return select_array

@cuda.jit
def extract_particles_from_gpu( x, y, z, ux, uy, uz, w, inv_gamma,
                                selected, part_idx_start ):
    """
    Extract a selection of particles from the GPU and 
    store them in a 2D array (8, N_part) in the following
    order: x, y, z, ux, uy, uz, w, inv_gamma.
    Selection goes from starting index (part_idx_start) 
    to (part_idx_start + N_part-1), where N_part is derived
    from the shape of the 2D array (selected).

    Parameters
    ----------
    x, y, z, ux, uy, uz, w, inv_gamma : 1D arrays of floats
        The GPU particle arrays for a given species.

    selected : 2D array of floats
        An empty GPU array to store the particles 
        that are extracted.
    
    part_idx_start : int
        The starting index needed for the extraction process.
        ( minimum particle index to be extracted )
    """

    i = cuda.grid(1)
    N_part = selected.shape[1]
 
    if i < N_part:
        ptcl_idx = part_idx_start+i
        selected[0, i] = x[ptcl_idx]
        selected[1, i] = y[ptcl_idx]
        selected[2, i] = z[ptcl_idx]
        selected[3, i] = ux[ptcl_idx]
        selected[4, i] = uy[ptcl_idx]
        selected[5, i] = uz[ptcl_idx]
        selected[6, i] = w[ptcl_idx]
        selected[7, i] = inv_gamma[ptcl_idx]