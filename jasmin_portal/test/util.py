"""
This module provides utilities for unit and integration testing using the
unittest module
"""

__author__ = "Matt Pryor"
__copyright__ = "Copyright 2015 UK Science and Technology Facilities Council"


import abc


class IntegrationTest(metaclass= abc.ABCMeta):
    """
    This class provides functionality for describing an integration test as
    a series of steps as a mixin
    
    All step methods (even the first!) should take an argument that is the
    result of the previous step
    If there is no previous step, or there was no return value, None will be given 
    """
    
    @abc.abstractmethod
    def steps(self):
        """
        Returns a list of method names for the steps of the integration test
        in the order that they must be executed
        """
    
    def test_integration(self):
        """
        Runs the steps for the integration test as sub-tests
        """
        result = None
        failed = False
        print()  # Print an empty line first
        for step in self.steps():
            print("    Running step: {} ...".format(step), end = " ", flush = True)
            # Use subTest for labelling output
            # However, we still want to exit on first failure, so we need to
            # check if an exception was thrown and terminate the loop
            with self.subTest(step):
                try:
                    method = getattr(self, step)
                    result = method(result)
                except Exception as e:
                    # Record that we need to exit the loop and rethrow
                    failed = True
                    raise e
            if failed:
                break
            else:
                print("ok")
