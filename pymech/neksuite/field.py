from __future__ import annotations

import io
import struct
import sys
from typing import Optional, Tuple

import numpy as np
from pydantic.dataclasses import ValidationError, dataclass
from pymech.core import HexaData
from pymech.log import logger


@dataclass
class Header:
    """Dataclass for Nek5000 field file header. This relies on the package
    pydantic_ and its ability to do type-checking and type-coercion of the
    header metadata.

    .. _pydantic: https://pydantic-docs.helpmanual.io/

    """

    # get word size: single or double precision
    wdsz: int
    # get polynomial order
    orders: Tuple[int, ...]
    # get number of elements
    nb_elems: int
    # get number of elements in the file
    nb_elems_file: int
    # get current time
    time: float
    # get current time step
    istep: int
    # get file id
    fid: int
    # get tot number of files
    nb_files: int

    # get variables [XUPTS[01-99]]
    variables: Optional[str] = None

    # floating point precision
    realtype: Optional[str] = None

    # compute total number of points per element
    nb_pts_elem: Optional[int] = None
    # get number of physical dimensions
    nb_dims: Optional[int] = None
    # get number of variables
    nb_vars: Optional[Tuple[int, ...]] = None

    def __post_init_post_parse__(self):
        # get word size: single or double precision
        wdsz = self.wdsz
        if not self.realtype:
            if wdsz == 4:
                self.realtype = "f"
            elif wdsz == 8:
                self.realtype = "d"
            else:
                logger.error(f"Could not interpret real type (wdsz = {wdsz})")

        orders = self.orders
        if not self.nb_pts_elem:
            self.nb_pts_elem = np.prod(orders)

        if not self.nb_dims:
            self.nb_dims = 2 + int(orders[2] > 1)

        if not self.variables and not self.nb_vars:
            raise ValidationError("Both variables and nb_vars cannot be None", self)
        elif self.variables:
            self.nb_vars = self._variables_to_nb_vars()
        elif self.nb_vars:
            self.variables = self._nb_vars_to_variables()

        logger.debug(f"Variables: {self.variables}, nb_vars: {self.nb_vars}")

    def _variables_to_nb_vars(self) -> Optional[Tuple[int, ...]]:
        # get variables [XUPTS[01-99]]
        variables = self.variables
        nb_dims = self.nb_dims

        if not variables:
            logger.error("Failed to convert variables to nb_vars")
            return None

        if not nb_dims:
            logger.error("Unintialized nb_dims")
            return None

        def nb_scalars():
            index_s = variables.index("S")
            return int(variables[index_s + 1 :])

        nb_vars = (
            nb_dims if "X" in variables else 0,
            nb_dims if "U" in variables else 0,
            1 if "P" in variables else 0,
            1 if "T" in variables else 0,
            nb_scalars() if "S" in variables else 0,
        )

        return nb_vars

    def _nb_vars_to_variables(self) -> Optional[str]:
        nb_vars = self.nb_vars
        if not nb_vars:
            logger.error("Failed to convert nb_vars to variables")
            return None

        str_vars = ("X", "U", "P", "T", f"S{nb_vars[4]:02d}")
        variables = (str_vars[i] if nb_vars[i] > 0 else "" for i in range(5))
        return "".join(variables)

    def as_bytestring(self) -> bytes:
        header = "#std %1i %2i %2i %2i %10i %10i %20.13E %9i %6i %6i %s" % (
            self.wdsz,
            self.orders[0],
            self.orders[1],
            self.orders[2],
            self.nb_elems,
            self.nb_elems_file,
            self.time,
            self.istep,
            self.fid,
            self.nb_files,
            self.variables,
        )
        return header.ljust(132).encode("utf-8")


def read_header(fp: io.BufferedReader) -> Header:
    """Make a :class:`pymech.neksuite.Header` instance from a file buffer
    opened in binary mode.

    """
    header = fp.read(132).split()
    logger.debug(b"Header: " + b" ".join(header))
    if len(header) < 12:
        raise IOError("Header of the file was too short.")

    return Header(header[1], header[2:5], *header[5:12])


# ==============================================================================
def readnek(fname, dtype="float64"):
    """A function for reading binary data from the nek5000 binary format

    Parameters
    ----------
    fname : str
        File name
    dtype : str or type
        Floating point data type. See also :class:`pymech.core.Elem`.
    """
    #
    try:
        infile = open(fname, "rb")
    except OSError as e:
        logger.critical(f"I/O error ({e.errno}): {e.strerror}")
        return -1
    #
    # ---------------------------------------------------------------------------
    # READ HEADER
    # ---------------------------------------------------------------------------
    #
    # read header
    h = read_header(infile)
    #
    # identify endian encoding
    etagb = infile.read(4)
    etagL = struct.unpack("<f", etagb)[0]
    etagL = int(etagL * 1e5) / 1e5
    etagB = struct.unpack(">f", etagb)[0]
    etagB = int(etagB * 1e5) / 1e5
    if etagL == 6.54321:
        logger.debug("Reading little-endian file\n")
        emode = "<"
    elif etagB == 6.54321:
        logger.debug("Reading big-endian file\n")
        emode = ">"
    else:
        logger.error("Could not interpret endianness")
        return -3
    #
    # read element map for the file
    elmap = infile.read(4 * h.nb_elems_file)
    elmap = struct.unpack(emode + h.nb_elems_file * "i", elmap)
    #
    # ---------------------------------------------------------------------------
    # READ DATA
    # ---------------------------------------------------------------------------
    #
    # initialize data structure
    data = HexaData(h.nb_dims, h.nb_elems, h.orders, h.nb_vars, 0, dtype)
    data.time = h.time
    data.istep = h.istep
    data.wdsz = h.wdsz
    data.elmap = np.array(elmap, dtype=np.int32)
    if emode == "<":
        data.endian = "little"
    elif emode == ">":
        data.endian = "big"

    def read_file_into_data(data_var, index_var):
        """Read binary file into an array attribute of ``data.elem``"""
        fi = infile.read(h.nb_pts_elem * h.wdsz)
        fi = np.frombuffer(fi, dtype=emode + h.realtype, count=h.nb_pts_elem)

        # Replace elem array in-place with
        # array read from file after reshaping as
        elem_shape = h.orders[::-1]  # lz, ly, lx
        data_var[index_var, ...] = fi.reshape(elem_shape)

    #
    # read geometry
    for iel in elmap:
        el = data.elem[iel - 1]
        for idim in range(h.nb_vars[0]):  # if 0, geometry is not read
            read_file_into_data(el.pos, idim)
    #
    # read velocity
    for iel in elmap:
        el = data.elem[iel - 1]
        for idim in range(h.nb_vars[1]):  # if 0, velocity is not read
            read_file_into_data(el.vel, idim)
    #
    # read pressure
    for iel in elmap:
        el = data.elem[iel - 1]
        for ivar in range(h.nb_vars[2]):  # if 0, pressure is not read
            read_file_into_data(el.pres, ivar)
    #
    # read temperature
    for iel in elmap:
        el = data.elem[iel - 1]
        for ivar in range(h.nb_vars[3]):  # if 0, temperature is not read
            read_file_into_data(el.temp, ivar)
    #
    # read scalar fields
    #
    # NOTE: This is not a bug!
    # Unlike other variables, scalars are in the outer loop and elements
    # are in the inner loop
    #
    for ivar in range(h.nb_vars[4]):  # if 0, scalars are not read
        for iel in elmap:
            el = data.elem[iel - 1]
            read_file_into_data(el.scal, ivar)
    #
    #
    # close file
    infile.close()
    #
    # output
    return data


# ==============================================================================
def writenek(fname, data):
    """A function for writing binary data in the nek5000 binary format

    Parameters
    ----------
    fname : str
            file name
    data : :class:`pymech.core.HexaData`
            data structure
    """
    #
    try:
        outfile = open(fname, "wb")
    except OSError as e:
        logger.critical(f"I/O error ({e.errno}): {e.strerror}")
        return -1
    #
    # ---------------------------------------------------------------------------
    # WRITE HEADER
    # ---------------------------------------------------------------------------
    #
    h = Header(
        data.wdsz,
        data.lr1,
        data.nel,
        data.nel,
        data.time,
        data.istep,
        fid=0,
        nb_files=1,
        nb_vars=data.var,
    )
    # NOTE: multiple files (not implemented). See fid, nb_files, nb_elem_file above
    #
    # get fields to be written
    #
    # get word size
    if h.wdsz == 4:
        logger.debug("Writing single-precision file")
    elif h.wdsz == 8:
        logger.debug("Writing double-precision file")
    else:
        logger.error("Could not interpret real type (wdsz = %i)" % (data.wdsz))
        return -2
    #
    # generate header
    outfile.write(h.as_bytestring())
    #
    # decide endianness
    if data.endian in ("big", "little"):
        byteswap = data.endian != sys.byteorder
        logger.debug(f"Writing {data.endian}-endian file")
    else:
        byteswap = False
        logger.warning(
            f"Unrecognized endianness {data.endian}, "
            f"writing native {sys.byteorder}-endian file"
        )

    def correct_endianness(a):
        """Return the array with the requested endianness"""
        if byteswap:
            return a.byteswap()
        else:
            return a

    #
    # write tag (to specify endianness)
    endianbytes = np.array([6.54321], dtype=np.float32)
    correct_endianness(endianbytes).tofile(outfile)
    #
    # write element map for the file
    correct_endianness(data.elmap).tofile(outfile)
    #
    # ---------------------------------------------------------------------------
    # WRITE DATA
    # ---------------------------------------------------------------------------
    #
    # compute total number of points per element
    #  npel = data.lr1[0] * data.lr1[1] * data.lr1[2]

    def write_ndarray_to_file(a):
        """Write a data array to the output file in the requested precision and endianness"""
        if data.wdsz == 4:
            correct_endianness(a.astype(np.float32)).tofile(outfile)
        else:
            correct_endianness(a).tofile(outfile)

    #
    # write geometry
    for iel in data.elmap:
        for idim in range(data.var[0]):  # if var[0] == 0, geometry is not written
            write_ndarray_to_file(data.elem[iel - 1].pos[idim, :, :, :])
    #
    # write velocity
    for iel in data.elmap:
        for idim in range(data.var[1]):  # if var[1] == 0, velocity is not written
            write_ndarray_to_file(data.elem[iel - 1].vel[idim, :, :, :])
    #
    # write pressure
    for iel in data.elmap:
        for ivar in range(data.var[2]):  # if var[2] == 0, pressure is not written
            write_ndarray_to_file(data.elem[iel - 1].pres[ivar, :, :, :])
    #
    # write temperature
    for iel in data.elmap:
        for ivar in range(data.var[3]):  # if var[3] == 0, temperature is not written
            write_ndarray_to_file(data.elem[iel - 1].temp[ivar, :, :, :])
    #
    # write scalars
    #
    # NOTE: This is not a bug!
    # Unlike other variables, scalars are in the outer loop and elements
    # are in the inner loop
    #
    for ivar in range(data.var[4]):  # if var[4] == 0, scalars are not written
        for iel in data.elmap:
            write_ndarray_to_file(data.elem[iel - 1].scal[ivar, :, :, :])
    #
    # write max and min of every field in every element (forced to single precision)
    if data.ndim == 3:
        #
        for iel in data.elmap:
            for idim in range(data.var[0]):
                correct_endianness(
                    np.min(data.elem[iel - 1].pos[idim, :, :, :]).astype(np.float32)
                ).tofile(outfile)
                correct_endianness(
                    np.max(data.elem[iel - 1].pos[idim, :, :, :]).astype(np.float32)
                ).tofile(outfile)
        for iel in data.elmap:
            for idim in range(data.var[1]):
                correct_endianness(
                    np.min(data.elem[iel - 1].vel[idim, :, :, :]).astype(np.float32)
                ).tofile(outfile)
                correct_endianness(
                    np.max(data.elem[iel - 1].vel[idim, :, :, :]).astype(np.float32)
                ).tofile(outfile)
        for iel in data.elmap:
            for ivar in range(data.var[2]):
                correct_endianness(
                    np.min(data.elem[iel - 1].pres[ivar, :, :, :]).astype(np.float32)
                ).tofile(outfile)
                correct_endianness(
                    np.max(data.elem[iel - 1].pres[ivar, :, :, :]).astype(np.float32)
                ).tofile(outfile)
        for iel in data.elmap:
            for ivar in range(data.var[3]):
                correct_endianness(
                    np.min(data.elem[iel - 1].temp[ivar, :, :, :]).astype(np.float32)
                ).tofile(outfile)
                correct_endianness(
                    np.max(data.elem[iel - 1].temp[ivar, :, :, :]).astype(np.float32)
                ).tofile(outfile)
        for iel in data.elmap:
            for ivar in range(data.var[4]):
                correct_endianness(
                    np.min(data.elem[iel - 1].scal[ivar, :, :, :]).astype(np.float32)
                ).tofile(outfile)
                correct_endianness(
                    np.max(data.elem[iel - 1].scal[ivar, :, :, :]).astype(np.float32)
                ).tofile(outfile)

    # close file
    outfile.close()
    #
    # output
    return 0