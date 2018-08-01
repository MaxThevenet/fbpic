"""
This file is part of the Fourier-Bessel Particle-In-Cell code (FB-PIC)
It imports the Particles object from the particles package, so that
this object can be used at a higher level.
"""

from .particles import Species, Particles
__all__ = ['Species', 'Particles']
