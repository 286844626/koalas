#
# Copyright (C) 2019 Databricks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
A wrapper class for Spark DataFrame to behave similar to pandas DataFrame.
"""
import warnings
from functools import partial, reduce
from typing import Any, List, Tuple, Union

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_dtype, is_datetime64tz_dtype, is_list_like
from pyspark import sql as spark
from pyspark.sql import functions as F, Column
from pyspark.sql.types import BooleanType, StructField, StructType, to_arrow_type
from pyspark.sql.utils import AnalysisException

from databricks import koalas as ks  # For running doctests and reference resolution in PyCharm.
from databricks.koalas.utils import default_session, validate_arguments_and_invoke_function
from databricks.koalas.exceptions import SparkPandasMergeError
from databricks.koalas.generic import _Frame, max_display_count
from databricks.koalas.metadata import Metadata
from databricks.koalas.missing.frame import _MissingPandasLikeDataFrame
from databricks.koalas.ml import corr
from databricks.koalas.selection import SparkDataFrameLocator
from databricks.koalas.typedef import infer_pd_series_spark_type


class DataFrame(_Frame):
    """
    Koala DataFrame that corresponds to Pandas DataFrame logically. This holds Spark DataFrame
    internally.

    :ivar _sdf: Spark Column instance
    :ivar _metadata: Metadata related to column names and index information.

    Parameters
    ----------
    data : numpy ndarray (structured or homogeneous), dict, Pandas DataFrame or Spark DataFrame
        Dict can contain Series, arrays, constants, or list-like objects
        If data is a dict, argument order is maintained for Python 3.6
        and later.
        Note that if `data` is a Pandas DataFrame, other arguments should not be used.
        If `data` is a Spark DataFrame, all other arguments except `index` should not be used.
    index : Index or array-like
        Index to use for resulting frame. Will default to RangeIndex if
        no indexing information part of input data and no index provided
        If `data` is a Spark DataFrame, `index` is expected to be `Metadata`.
    columns : Index or array-like
        Column labels to use for resulting frame. Will default to
        RangeIndex (0, 1, 2, ..., n) if no column labels are provided
    dtype : dtype, default None
        Data type to force. Only a single dtype is allowed. If None, infer
    copy : boolean, default False
        Copy data from inputs. Only affects DataFrame / 2d ndarray input

    Examples
    --------
    Constructing DataFrame from a dictionary.

    >>> d = {'col1': [1, 2], 'col2': [3, 4]}
    >>> df = ks.DataFrame(data=d, columns=['col1', 'col2'])
    >>> df
       col1  col2
    0     1     3
    1     2     4

    Constructing DataFrame from Pandas DataFrame

    >>> df = ks.DataFrame(pd.DataFrame(data=d, columns=['col1', 'col2']))
    >>> df
       col1  col2
    0     1     3
    1     2     4

    Notice that the inferred dtype is int64.

    >>> df.dtypes
    col1    int64
    col2    int64
    dtype: object

    To enforce a single dtype:

    >>> df = ks.DataFrame(data=d, dtype=np.int8)
    >>> df.dtypes
    col1    int8
    col2    int8
    dtype: object

    Constructing DataFrame from numpy ndarray:

    >>> df2 = ks.DataFrame(np.random.randint(low=0, high=10, size=(5, 5)),
    ...                    columns=['a', 'b', 'c', 'd', 'e'])
    >>> df2  # doctest: +SKIP
       a  b  c  d  e
    0  3  1  4  9  8
    1  4  8  4  8  4
    2  7  6  5  6  7
    3  8  7  9  1  0
    4  2  5  4  3  9
    """
    def __init__(self, data=None, index=None, columns=None, dtype=None, copy=False):
        if isinstance(data, pd.DataFrame):
            assert index is None
            assert columns is None
            assert dtype is None
            assert not copy
            self._init_from_pandas(data)
        elif isinstance(data, spark.DataFrame):
            assert columns is None
            assert dtype is None
            assert not copy
            self._init_from_spark(data, index)
        else:
            pdf = pd.DataFrame(data=data, index=index, columns=columns, dtype=dtype, copy=copy)
            self._init_from_pandas(pdf)

    def _init_from_pandas(self, pdf):
        metadata = Metadata.from_pandas(pdf)
        reset_index = pdf.reset_index()
        reset_index.columns = metadata.columns
        schema = StructType([StructField(name, infer_pd_series_spark_type(col),
                                         nullable=bool(col.isnull().any()))
                             for name, col in reset_index.iteritems()])
        for name, col in reset_index.iteritems():
            dt = col.dtype
            if is_datetime64_dtype(dt) or is_datetime64tz_dtype(dt):
                continue
            reset_index[name] = col.replace({np.nan: None})
        self._init_from_spark(default_session().createDataFrame(reset_index, schema=schema),
                              metadata)

    def _init_from_spark(self, sdf, metadata=None):
        self._sdf = sdf
        if metadata is None:
            self._metadata = Metadata(data_columns=self._sdf.schema.fieldNames())
        else:
            self._metadata = metadata

    @property
    def _index_columns(self):
        return [self._sdf.__getitem__(field)
                for field in self._metadata.index_columns]

    def _reduce_for_stat_function(self, sfun):
        """
        Applies sfun to each column and returns a pd.Series where the number of rows equal the
        number of columns.

        :param sfun: either an 1-arg function that takes a Column and returns a Column, or
        a 2-arg function that takes a Column and its DataType and returns a Column.
        """
        from inspect import signature
        exprs = []
        num_args = len(signature(sfun).parameters)
        for col in self.columns:
            col_sdf = self._sdf[col]
            col_type = self._sdf.schema[col].dataType
            if isinstance(col_type, BooleanType) and sfun.__name__ not in ('min', 'max'):
                # Stat functions cannot be used with boolean values by default
                # Thus, cast to integer (true to 1 and false to 0)
                # Exclude the min and max methods though since those work with booleans
                col_sdf = col_sdf.cast('integer')
            if num_args == 1:
                # Only pass in the column if sfun accepts only one arg
                col_sdf = sfun(col_sdf)
            else:  # must be 2
                assert num_args == 2
                # Pass in both the column and its data type if sfun accepts two args
                col_sdf = sfun(col_sdf, col_type)
            exprs.append(col_sdf.alias(col))

        sdf = self._sdf.select(*exprs)
        pdf = sdf.toPandas()
        assert len(pdf) == 1, (sdf, pdf)
        row = pdf.iloc[0]
        row.name = None
        return row  # Return first row as a Series

    def corr(self, method='pearson'):
        """
        Compute pairwise correlation of columns, excluding NA/null values.

        Parameters
        ----------
        method : {'pearson', 'spearman'}
            * pearson : standard correlation coefficient
            * spearman : Spearman rank correlation

        Returns
        -------
        y : pandas.DataFrame

        See Also
        --------
        Series.corr

        Examples
        --------
        >>> df = ks.DataFrame([(.2, .3), (.0, .6), (.6, .0), (.2, .1)],
        ...                   columns=['dogs', 'cats'])
        >>> df.corr('pearson')
                  dogs      cats
        dogs  1.000000 -0.851064
        cats -0.851064  1.000000

        >>> df.corr('spearman')
                  dogs      cats
        dogs  1.000000 -0.948683
        cats -0.948683  1.000000

        Notes
        -----
        There are behavior differences between Koalas and pandas.

        * the `method` argument only accepts 'pearson', 'spearman'
        * the data should not contain NaNs. Koalas will return an error.
        * Koalas doesn't support the following argument(s).

          * `min_periods` argument is not supported
        """
        return corr(self, method)

    def iteritems(self):
        """
        Iterator over (column name, Series) pairs.

        Iterates over the DataFrame columns, returning a tuple with
        the column name and the content as a Series.

        Returns
        -------
        label : object
            The column names for the DataFrame being iterated over.
        content : Series
            The column entries belonging to each label, as a Series.

        Examples
        --------
        >>> df = ks.DataFrame({'species': ['bear', 'bear', 'marsupial'],
        ...                    'population': [1864, 22000, 80000]},
        ...                   index=['panda', 'polar', 'koala'],
        ...                   columns=['species', 'population'])
        >>> df
                 species  population
        panda       bear        1864
        polar       bear       22000
        koala  marsupial       80000

        >>> for label, content in df.iteritems():
        ...    print('label:', label)
        ...    print('content:', content.to_string())
        ...
        label: species
        content: panda         bear
        polar         bear
        koala    marsupial
        label: population
        content: panda     1864
        polar    22000
        koala    80000
        """
        cols = list(self.columns)
        return list((col_name, self[col_name]) for col_name in cols)

    def to_html(self, buf=None, columns=None, col_space=None, header=True, index=True,
                na_rep='NaN', formatters=None, float_format=None, sparsify=None, index_names=True,
                justify=None, max_rows=None, max_cols=None, show_dimensions=False, decimal='.',
                bold_rows=True, classes=None, escape=True, notebook=False, border=None,
                table_id=None, render_links=False):
        """
        Render a DataFrame as an HTML table.

        .. note:: This method should only be used if the resulting Pandas object is expected
                  to be small, as all the data is loaded into the driver's memory. If the input
                  is large, set max_rows parameter.

        Parameters
        ----------
        buf : StringIO-like, optional
            Buffer to write to.
        columns : sequence, optional, default None
            The subset of columns to write. Writes all columns by default.
        col_space : int, optional
            The minimum width of each column.
        header : bool, optional
            Write out the column names. If a list of strings is given, it
            is assumed to be aliases for the column names
        index : bool, optional, default True
            Whether to print index (row) labels.
        na_rep : str, optional, default 'NaN'
            String representation of NAN to use.
        formatters : list or dict of one-param. functions, optional
            Formatter functions to apply to columns' elements by position or
            name.
            The result of each function must be a unicode string.
            List must be of length equal to the number of columns.
        float_format : one-parameter function, optional, default None
            Formatter function to apply to columns' elements if they are
            floats. The result of this function must be a unicode string.
        sparsify : bool, optional, default True
            Set to False for a DataFrame with a hierarchical index to print
            every multiindex key at each row.
        index_names : bool, optional, default True
            Prints the names of the indexes.
        justify : str, default None
            How to justify the column labels. If None uses the option from
            the print configuration (controlled by set_option), 'right' out
            of the box. Valid values are

            * left
            * right
            * center
            * justify
            * justify-all
            * start
            * end
            * inherit
            * match-parent
            * initial
            * unset.
        max_rows : int, optional
            Maximum number of rows to display in the console.
        max_cols : int, optional
            Maximum number of columns to display in the console.
        show_dimensions : bool, default False
            Display DataFrame dimensions (number of rows by number of columns).
        decimal : str, default '.'
            Character recognized as decimal separator, e.g. ',' in Europe.
        bold_rows : bool, default True
            Make the row labels bold in the output.
        classes : str or list or tuple, default None
            CSS class(es) to apply to the resulting html table.
        escape : bool, default True
            Convert the characters <, >, and & to HTML-safe sequences.
        notebook : {True, False}, default False
            Whether the generated HTML is for IPython Notebook.
        border : int
            A ``border=border`` attribute is included in the opening
            `<table>` tag. Default ``pd.options.html.border``.
        table_id : str, optional
            A css id is included in the opening `<table>` tag if specified.
        render_links : bool, default False
            Convert URLs to HTML links (only works with Pandas 0.24+).

        Returns
        -------
        str (or unicode, depending on data and options)
            String representation of the dataframe.

        See Also
        --------
        to_string : Convert DataFrame to a string.
        """
        # Make sure locals() call is at the top of the function so we don't capture local variables.
        args = locals()
        if max_rows is not None:
            kdf = self.head(max_rows)
        else:
            kdf = self

        return validate_arguments_and_invoke_function(
            kdf.to_pandas(), self.to_html, pd.DataFrame.to_html, args)

    def to_string(self, buf=None, columns=None, col_space=None, header=True,
                  index=True, na_rep='NaN', formatters=None, float_format=None,
                  sparsify=None, index_names=True, justify=None,
                  max_rows=None, max_cols=None, show_dimensions=False,
                  decimal='.', line_width=None):
        """
        Render a DataFrame to a console-friendly tabular output.

        .. note:: This method should only be used if the resulting Pandas object is expected
                  to be small, as all the data is loaded into the driver's memory. If the input
                  is large, set max_rows parameter.

        Parameters
        ----------
        buf : StringIO-like, optional
            Buffer to write to.
        columns : sequence, optional, default None
            The subset of columns to write. Writes all columns by default.
        col_space : int, optional
            The minimum width of each column.
        header : bool, optional
            Write out the column names. If a list of strings is given, it
            is assumed to be aliases for the column names
        index : bool, optional, default True
            Whether to print index (row) labels.
        na_rep : str, optional, default 'NaN'
            String representation of NAN to use.
        formatters : list or dict of one-param. functions, optional
            Formatter functions to apply to columns' elements by position or
            name.
            The result of each function must be a unicode string.
            List must be of length equal to the number of columns.
        float_format : one-parameter function, optional, default None
            Formatter function to apply to columns' elements if they are
            floats. The result of this function must be a unicode string.
        sparsify : bool, optional, default True
            Set to False for a DataFrame with a hierarchical index to print
            every multiindex key at each row.
        index_names : bool, optional, default True
            Prints the names of the indexes.
        justify : str, default None
            How to justify the column labels. If None uses the option from
            the print configuration (controlled by set_option), 'right' out
            of the box. Valid values are

            * left
            * right
            * center
            * justify
            * justify-all
            * start
            * end
            * inherit
            * match-parent
            * initial
            * unset.
        max_rows : int, optional
            Maximum number of rows to display in the console.
        max_cols : int, optional
            Maximum number of columns to display in the console.
        show_dimensions : bool, default False
            Display DataFrame dimensions (number of rows by number of columns).
        decimal : str, default '.'
            Character recognized as decimal separator, e.g. ',' in Europe.
        line_width : int, optional
            Width to wrap a line in characters.

        Returns
        -------
        str (or unicode, depending on data and options)
            String representation of the dataframe.

        See Also
        --------
        to_html : Convert DataFrame to HTML.

        Examples
        --------
        >>> df = ks.DataFrame({'col1': [1, 2, 3], 'col2': [4, 5, 6]}, columns=['col1', 'col2'])
        >>> print(df.to_string())
           col1  col2
        0     1     4
        1     2     5
        2     3     6

        >>> print(df.to_string(max_rows=2))
           col1  col2
        0     1     4
        1     2     5
        """
        # Make sure locals() call is at the top of the function so we don't capture local variables.
        args = locals()
        if max_rows is not None:
            kdf = self.head(max_rows)
        else:
            kdf = self

        return validate_arguments_and_invoke_function(
            kdf.to_pandas(), self.to_string, pd.DataFrame.to_string, args)

    def to_json(self, path_or_buf=None, orient=None, date_format=None,
                double_precision=10, force_ascii=True, date_unit='ms',
                default_handler=None, lines=False, compression='infer',
                index=True):
        """
        Convert the object to a JSON string.

        Note NaN's and None will be converted to null and datetime objects
        will be converted to UNIX timestamps.

        .. note:: This method should only be used if the resulting JSON is expected
            to be small, as all the data is loaded into the driver's memory.

        Parameters
        ----------
        path_or_buf : string or file handle, optional
            File path or object. If not specified, the result is returned as
            a string.
        orient : string
            Indication of expected JSON string format.

            * Series

              - default is 'index'
              - allowed values are: {'split','records','index','table'}

            * DataFrame

              - default is 'columns'
              - allowed values are:
                {'split','records','index','columns','values','table'}

            * The format of the JSON string

              - 'split' : dict like {'index' -> [index],
                'columns' -> [columns], 'data' -> [values]}
              - 'records' : list like
                [{column -> value}, ... , {column -> value}]
              - 'index' : dict like {index -> {column -> value}}
              - 'columns' : dict like {column -> {index -> value}}
              - 'values' : just the values array
              - 'table' : dict like {'schema': {schema}, 'data': {data}}
                describing the data, and the data component is
                like ``orient='records'``.
        date_format : {None, 'epoch', 'iso'}
            Type of date conversion. 'epoch' = epoch milliseconds,
            'iso' = ISO8601. The default depends on the `orient`. For
            ``orient='table'``, the default is 'iso'. For all other orients,
            the default is 'epoch'.
        double_precision : int, default 10
            The number of decimal places to use when encoding
            floating point values.
        force_ascii : bool, default True
            Force encoded string to be ASCII.
        date_unit : string, default 'ms' (milliseconds)
            The time unit to encode to, governs timestamp and ISO8601
            precision.  One of 's', 'ms', 'us', 'ns' for second, millisecond,
            microsecond, and nanosecond respectively.
        default_handler : callable, default None
            Handler to call if object cannot otherwise be converted to a
            suitable format for JSON. Should receive a single argument which is
            the object to convert and return a serialisable object.
        lines : bool, default False
            If 'orient' is 'records' write out line delimited json format. Will
            throw ValueError if incorrect 'orient' since others are not list
            like.
        compression : {'infer', 'gzip', 'bz2', 'zip', 'xz', None}
            A string representing the compression to use in the output file,
            only used when the first argument is a filename. By default, the
            compression is inferred from the filename.
        index : bool, default True
            Whether to include the index values in the JSON string. Not
            including the index (``index=False``) is only supported when
            orient is 'split' or 'table'.

        Examples
        --------

        >>> df = ks.DataFrame([['a', 'b'], ['c', 'd']],
        ...                   index=['row 1', 'row 2'],
        ...                   columns=['col 1', 'col 2'])
        >>> df.to_json(orient='split')
        '{"columns":["col 1","col 2"],\
"index":["row 1","row 2"],\
"data":[["a","b"],["c","d"]]}'

        Encoding/decoding a Dataframe using ``'records'`` formatted JSON.
        Note that index labels are not preserved with this encoding.

        >>> df.to_json(orient='records')
        '[{"col 1":"a","col 2":"b"},{"col 1":"c","col 2":"d"}]'

        Encoding/decoding a Dataframe using ``'index'`` formatted JSON:

        >>> df.to_json(orient='index')
        '{"row 1":{"col 1":"a","col 2":"b"},"row 2":{"col 1":"c","col 2":"d"}}'

        Encoding/decoding a Dataframe using ``'columns'`` formatted JSON:

        >>> df.to_json(orient='columns')
        '{"col 1":{"row 1":"a","row 2":"c"},"col 2":{"row 1":"b","row 2":"d"}}'

        Encoding/decoding a Dataframe using ``'values'`` formatted JSON:

        >>> df.to_json(orient='values')
        '[["a","b"],["c","d"]]'

        Encoding with Table Schema

        >>> df.to_json(orient='table') # doctest: +SKIP
        '{"schema": {"fields":[{"name":"index","type":"string"},\
{"name":"col 1","type":"string"},\
{"name":"col 2","type":"string"}],\
"primaryKey":["index"],\
"pandas_version":"0.20.0"}, \
"data": [{"index":"row 1","col 1":"a","col 2":"b"},\
{"index":"row 2","col 1":"c","col 2":"d"}]}'
        """
        # Make sure locals() call is at the top of the function so we don't capture local variables.
        args = locals()
        kdf = self
        return validate_arguments_and_invoke_function(
            kdf.to_pandas(), self.to_json, pd.DataFrame.to_json, args)

    def to_dict(self, orient='dict', into=dict):
        """
        Convert the DataFrame to a dictionary.

        The type of the key-value pairs can be customized with the parameters
        (see below).

        .. note:: This method should only be used if the resulting Pandas DataFrame is expected
            to be small, as all the data is loaded into the driver's memory.

        Parameters
        ----------
        orient : str {'dict', 'list', 'series', 'split', 'records', 'index'}
            Determines the type of the values of the dictionary.

            - 'dict' (default) : dict like {column -> {index -> value}}
            - 'list' : dict like {column -> [values]}
            - 'series' : dict like {column -> Series(values)}
            - 'split' : dict like
              {'index' -> [index], 'columns' -> [columns], 'data' -> [values]}
            - 'records' : list like
              [{column -> value}, ... , {column -> value}]
            - 'index' : dict like {index -> {column -> value}}

            Abbreviations are allowed. `s` indicates `series` and `sp`
            indicates `split`.

        into : class, default dict
            The collections.abc.Mapping subclass used for all Mappings
            in the return value.  Can be the actual class or an empty
            instance of the mapping type you want.  If you want a
            collections.defaultdict, you must pass it initialized.

        Returns
        -------
        dict, list or collections.abc.Mapping
            Return a collections.abc.Mapping object representing the DataFrame.
            The resulting transformation depends on the `orient` parameter.

        Examples
        --------
        >>> df = ks.DataFrame({'col1': [1, 2],
        ...                    'col2': [0.5, 0.75]},
        ...                   index=['row1', 'row2'],
        ...                   columns=['col1', 'col2'])
        >>> df
              col1  col2
        row1     1  0.50
        row2     2  0.75
        >>> df_dict = df.to_dict()
        >>> sorted([(key, sorted(values.items())) for key, values in df_dict.items()])
        [('col1', [('row1', 1), ('row2', 2)]), ('col2', [('row1', 0.5), ('row2', 0.75)])]

        You can specify the return orientation.

        >>> df_dict = df.to_dict('series')
        >>> sorted(df_dict.items())
        [('col1', row1    1
        row2    2
        Name: col1, dtype: int64), ('col2', row1    0.50
        row2    0.75
        Name: col2, dtype: float64)]
        >>> df_dict = df.to_dict('split')
        >>> sorted(df_dict.items())  # doctest: +ELLIPSIS
        [('columns', ['col1', 'col2']), ('data', [[1..., 0.75]]), ('index', ['row1', 'row2'])]

        >>> df_dict = df.to_dict('records')
        >>> [sorted(values.items()) for values in df_dict]  # doctest: +ELLIPSIS
        [[('col1', 1...), ('col2', 0.5)], [('col1', 2...), ('col2', 0.75)]]

        >>> df_dict = df.to_dict('index')
        >>> sorted([(key, sorted(values.items())) for key, values in df_dict.items()])
        [('row1', [('col1', 1), ('col2', 0.5)]), ('row2', [('col1', 2), ('col2', 0.75)])]

        You can also specify the mapping type.

        >>> from collections import OrderedDict, defaultdict
        >>> df.to_dict(into=OrderedDict)
        OrderedDict([('col1', OrderedDict([('row1', 1), ('row2', 2)])), \
('col2', OrderedDict([('row1', 0.5), ('row2', 0.75)]))])

        If you want a `defaultdict`, you need to initialize it:

        >>> dd = defaultdict(list)
        >>> df.to_dict('records', into=dd)  # doctest: +ELLIPSIS
        [defaultdict(<class 'list'>, {'col..., 'col...}), \
defaultdict(<class 'list'>, {'col..., 'col...})]
        """
        # Make sure locals() call is at the top of the function so we don't capture local variables.
        args = locals()
        kdf = self
        return validate_arguments_and_invoke_function(
            kdf.to_pandas(), self.to_dict, pd.DataFrame.to_dict, args)

    def to_csv(self, path_or_buf=None, sep=",", na_rep='', float_format=None,
               columns=None, header=True, index=True, index_label=None,
               mode='w', encoding=None, compression='infer', quoting=None,
               quotechar='"', line_terminator="\n", chunksize=None,
               tupleize_cols=None, date_format=None, doublequote=True,
               escapechar=None, decimal='.'):
        """
        Write object to a comma-separated values (csv) file.

        .. note:: This method should only be used if the resulting CSV is expected
            to be small, as all the data is loaded into the driver's memory.

        Parameters
        ----------
        path_or_buf : str or file handle, default None
            File path or object, if None is provided the result is returned as
            a string.  If a file object is passed it should be opened with
            `newline=''`, disabling universal newlines.

        sep : str, default ','
            String of length 1. Field delimiter for the output file.
        na_rep : str, default ''
            Missing data representation.
        float_format : str, default None
            Format string for floating point numbers.
        columns : sequence, optional
            Columns to write.
        header : bool or list of str, default True
            Write out the column names. If a list of strings is given it is
            assumed to be aliases for the column names.
        index : bool, default True
            Write row names (index).
        index_label : str or sequence, or False, default None
            Column label for index column(s) if desired. If None is given, and
            `header` and `index` are True, then the index names are used. A
            sequence should be given if the object uses MultiIndex. If
            False do not print fields for index names. Use index_label=False
            for easier importing in R.
        mode : str
            Python write mode, default 'w'.
        encoding : str, optional
            A string representing the encoding to use in the output file,
            defaults to 'ascii' on Python 2 and 'utf-8' on Python 3.
        compression : str, default 'infer'
            Compression mode among the following possible values: {'infer',
            'gzip', 'bz2', 'zip', 'xz', None}. If 'infer' and `path_or_buf`
            is path-like, then detect compression from the following
            extensions: '.gz', '.bz2', '.zip' or '.xz'. (otherwise no
            compression).
        quoting : optional constant from csv module
            Defaults to csv.QUOTE_MINIMAL. If you have set a `float_format`
            then floats are converted to strings and thus csv.QUOTE_NONNUMERIC
            will treat them as non-numeric.
        quotechar : str, default '\"'
            String of length 1. Character used to quote fields.
        line_terminator : string, default '\\n'
            The newline character or character sequence to use in the output
            file. Defaults to `os.linesep`, which depends on the OS in which
            this method is called ('\n' for linux, '\r\n' for Windows, i.e.).
        chunksize : int or None
            Rows to write at a time.
        tupleize_cols : bool, default False
            Write MultiIndex columns as a list of tuples (if True) or in
            the new, expanded format, where each MultiIndex column is a row
            in the CSV (if False).
        date_format : str, default None
            Format string for datetime objects.
        doublequote : bool, default True
            Control quoting of `quotechar` inside a field.
        escapechar : str, default None
            String of length 1. Character used to escape `sep` and `quotechar`
            when appropriate.
        decimal : str, default '.'
            Character recognized as decimal separator. E.g. use ',' for
            European data.

        Returns
        -------
        None or str
            If path_or_buf is None, returns the resulting csv format as a
            string. Otherwise returns None.

        Examples
        --------
        >>> df = ks.DataFrame({'name': ['Raphael', 'Donatello'],
        ...                    'mask': ['red', 'purple'],
        ...                    'weapon': ['sai', 'bo staff']},
        ...                     columns=['name', 'mask', 'weapon'])
        >>> df.to_csv(index=False)
        'name,mask,weapon\\nRaphael,red,sai\\nDonatello,purple,bo staff\\n'
        """

        # Make sure locals() call is at the top of the function so we don't capture local variables.
        args = locals()
        kdf = self

        return validate_arguments_and_invoke_function(
            kdf.to_pandas(), self.to_csv, pd.DataFrame.to_csv, args)

    @property
    def index(self):
        """The index (row labels) Column of the DataFrame.

        Currently supported only when the DataFrame has a single index.
        """
        from databricks.koalas.series import Series
        if len(self._metadata.index_map) != 1:
            raise KeyError('Currently supported only when the DataFrame has a single index.')
        return Series(self._index_columns[0], anchor=self, index=[])

    def set_index(self, keys, drop=True, append=False, inplace=False):
        """Set the DataFrame index (row labels) using one or more existing columns. By default
        yields a new object.

        :param keys: column label or list of column labels / arrays
        :param drop: boolean, default True
                     Delete columns to be used as the new index
        :param append: boolean, default False
                       Whether to append columns to existing index
        :param inplace: boolean, default False
                        Modify the DataFrame in place (do not create a new object)
        :return: :class:`DataFrame`
        """
        if isinstance(keys, str):
            keys = [keys]
        else:
            keys = list(keys)
        for key in keys:
            if key not in self.columns:
                raise KeyError(key)

        if drop:
            data_columns = [column for column in self._metadata.data_columns if column not in keys]
        else:
            data_columns = self._metadata.data_columns
        if append:
            index_map = self._metadata.index_map + [(column, column) for column in keys]
        else:
            index_map = [(column, column) for column in keys]

        metadata = self._metadata.copy(data_columns=data_columns, index_map=index_map)

        # Sync Spark's columns as well.
        sdf = self._sdf.select(['`{}`'.format(name) for name in metadata.columns])

        if inplace:
            self._metadata = metadata
            self._sdf = sdf
        else:
            kdf = self.copy()
            kdf._metadata = metadata
            kdf._sdf = sdf
            return kdf

    def reset_index(self, level=None, drop=False, inplace=False):
        """For DataFrame with multi-level index, return new DataFrame with labeling information in
        the columns under the index names, defaulting to 'level_0', 'level_1', etc. if any are None.
        For a standard index, the index name will be used (if set), otherwise a default 'index' or
        'level_0' (if 'index' is already taken) will be used.

        :param level: int, str, tuple, or list, default None
                      Only remove the given levels from the index. Removes all levels by default
        :param drop: boolean, default False
                     Do not try to insert index into dataframe columns. This resets the index to the
                     default integer index.
        :param inplace: boolean, default False
                        Modify the DataFrame in place (do not create a new object)
        :return: :class:`DataFrame`
        """
        if len(self._metadata.index_map) == 0:
            raise NotImplementedError('Can\'t reset index because there is no index.')

        multi_index = len(self._metadata.index_map) > 1

        def rename(index):
            if multi_index:
                return 'level_{}'.format(index)
            else:
                if 'index' not in self._metadata.data_columns:
                    return 'index'
                else:
                    return 'level_{}'.format(index)

        if level is None:
            new_index_map = [(column, name if name is not None else rename(i))
                             for i, (column, name) in enumerate(self._metadata.index_map)]
            index_map = []
        else:
            if isinstance(level, (int, str)):
                level = [level]
            level = list(level)

            if all(isinstance(l, int) for l in level):
                for lev in level:
                    if lev >= len(self._metadata.index_map):
                        raise IndexError('Too many levels: Index has only {} level, not {}'
                                         .format(len(self._metadata.index_map), lev + 1))
                idx = level
            elif all(isinstance(lev, str) for lev in level):
                idx = []
                for l in level:
                    try:
                        i = self._metadata.index_columns.index(l)
                        idx.append(i)
                    except ValueError:
                        if multi_index:
                            raise KeyError('Level unknown not found')
                        else:
                            raise KeyError('Level unknown must be same as name ({})'
                                           .format(self._metadata.index_columns[0]))
            else:
                raise ValueError('Level should be all int or all string.')
            idx.sort()

            new_index_map = []
            index_map = self._metadata.index_map.copy()
            for i in idx:
                info = self._metadata.index_map[i]
                index_column, index_name = info
                new_index_map.append(
                    (index_column,
                     index_name if index_name is not None else rename(index_name)))
                index_map.remove(info)

        if drop:
            new_index_map = []

        metadata = self._metadata.copy(
            data_columns=[column for column, _ in new_index_map] + self._metadata.data_columns,
            index_map=index_map)
        columns = [name for _, name in new_index_map] + self._metadata.data_columns
        if inplace:
            self._metadata = metadata
            self.columns = columns
        else:
            kdf = self.copy()
            kdf._metadata = metadata
            kdf.columns = columns
            return kdf

    def isnull(self):
        """
        Detects missing values for items in the current Dataframe.

        Return a boolean same-sized Dataframe indicating if the values are NA.
        NA values, such as None or numpy.NaN, gets mapped to True values.
        Everything else gets mapped to False values.

        See Also
        --------
        Dataframe.notnull

        Examples
        --------
        >>> df = ks.DataFrame([(.2, .3), (.0, None), (.6, None), (.2, .1)])
        >>> df.isnull()
               0      1
        0  False  False
        1  False   True
        2  False   True
        3  False  False

        >>> df = ks.DataFrame([[None, 'bee', None], ['dog', None, 'fly']])
        >>> df.isnull()
               0      1      2
        0   True  False   True
        1  False   True  False
        """
        kdf = self.copy()
        for name, ks in kdf.iteritems():
            kdf[name] = ks.isnull()
        return kdf

    isna = isnull

    def notnull(self):
        """
        Detects non-missing values for items in the current Dataframe.

        This function takes a dataframe and indicates whether it's
        values are valid (not missing, which is ``NaN`` in numeric
        datatypes, ``None`` or ``NaN`` in objects and ``NaT`` in datetimelike).

        See Also
        --------
        Dataframe.isnull

        Examples
        --------
        >>> df = ks.DataFrame([(.2, .3), (.0, None), (.6, None), (.2, .1)])
        >>> df.notnull()
              0      1
        0  True   True
        1  True  False
        2  True  False
        3  True   True

        >>> df = ks.DataFrame([['ant', 'bee', 'cat'], ['dog', None, 'fly']])
        >>> df.notnull()
              0      1     2
        0  True   True  True
        1  True  False  True
        """
        kdf = self.copy()
        for name, ks in kdf.iteritems():
            kdf[name] = ks.notnull()
        return kdf

    notna = notnull

    def to_koalas(self):
        """
        Converts the existing DataFrame into a Koalas DataFrame.

        This method is monkey-patched into Spark's DataFrame and can be used
        to convert a Spark DataFrame into a Koalas DataFrame. If running on
        an existing Koalas DataFrame, the method returns itself.

        If a Koalas DataFrame is converted to a Spark DataFrame and then back
        to Koalas, it will lose the index information and the original index
        will be turned into a normal column.

        See Also
        --------
        DataFrame.to_spark

        Examples
        --------
        >>> df = ks.DataFrame({'col1': [1, 2], 'col2': [3, 4]}, columns=['col1', 'col2'])
        >>> df
           col1  col2
        0     1     3
        1     2     4

        >>> spark_df = df.to_spark()
        >>> spark_df
        DataFrame[__index_level_0__: bigint, col1: bigint, col2: bigint]

        >>> kdf = spark_df.to_koalas()
        >>> kdf
           __index_level_0__  col1  col2
        0                  0     1     3
        1                  1     2     4

        Calling to_koalas on a Koalas DataFrame simply returns itself.

        >>> df.to_koalas()
           col1  col2
        0     1     3
        1     2     4
        """
        if isinstance(self, DataFrame):
            return self
        else:
            return DataFrame(self)

    def to_spark(self):
        """
        Return the current DataFrame as a Spark DataFrame.

        See Also
        --------
        DataFrame.to_koalas
        """
        return self._sdf

    def to_pandas(self):
        """
        Return a Pandas DataFrame.

        .. note:: This method should only be used if the resulting Pandas DataFrame is expected
            to be small, as all the data is loaded into the driver's memory.

        Examples
        --------
        >>> df = ks.DataFrame([(.2, .3), (.0, .6), (.6, .0), (.2, .1)],
        ...                   columns=['dogs', 'cats'])
        >>> df.to_pandas()
           dogs  cats
        0   0.2   0.3
        1   0.0   0.6
        2   0.6   0.0
        3   0.2   0.1
        """
        sdf = self._sdf.select(['`{}`'.format(name) for name in self._metadata.columns])
        pdf = sdf.toPandas()
        if len(pdf) == 0 and len(sdf.schema) > 0:
            # TODO: push to OSS
            pdf = pdf.astype({field.name: to_arrow_type(field.dataType).to_pandas_dtype()
                              for field in sdf.schema})

        index_columns = self._metadata.index_columns
        if len(index_columns) > 0:
            append = False
            for index_field in index_columns:
                drop = index_field not in self._metadata.data_columns
                pdf = pdf.set_index(index_field, drop=drop, append=append)
                append = True
            pdf = pdf[self._metadata.data_columns]

        index_names = self._metadata.index_names
        if len(index_names) > 0:
            if isinstance(pdf.index, pd.MultiIndex):
                pdf.index.names = index_names
            else:
                pdf.index.name = index_names[0]
        return pdf

    # Alias to maintain backward compatibility with Spark
    toPandas = to_pandas

    def assign(self, **kwargs):
        """
        Assign new columns to a DataFrame.

        Returns a new object with all original columns in addition to new ones.
        Existing columns that are re-assigned will be overwritten.

        Parameters
        ----------
        **kwargs : dict of {str: callable or Series}
            The column names are keywords. If the values are
            callable, they are computed on the DataFrame and
            assigned to the new columns. The callable must not
            change input DataFrame (though Koalas doesn't check it).
            If the values are not callable, (e.g. a Series or a literal),
            they are simply assigned.

        Returns
        -------
        DataFrame
            A new DataFrame with the new columns in addition to
            all the existing columns.

        Examples
        --------
        >>> df = ks.DataFrame({'temp_c': [17.0, 25.0]},
        ...                   index=['Portland', 'Berkeley'])
        >>> df
                  temp_c
        Portland    17.0
        Berkeley    25.0

        Where the value is a callable, evaluated on `df`:

        >>> df.assign(temp_f=lambda x: x.temp_c * 9 / 5 + 32)
                  temp_c  temp_f
        Portland    17.0    62.6
        Berkeley    25.0    77.0

        Alternatively, the same behavior can be achieved by directly
        referencing an existing Series or sequence and you can also
        create multiple columns within the same assign.

        >>> assigned = df.assign(temp_f=df['temp_c'] * 9 / 5 + 32,
        ...                      temp_k=df['temp_c'] + 273.15)
        >>> assigned[['temp_c', 'temp_f', 'temp_k']]
                  temp_c  temp_f  temp_k
        Portland    17.0    62.6  290.15
        Berkeley    25.0    77.0  298.15

        Notes
        -----
        Assigning multiple columns within the same ``assign`` is possible
        but you cannot refer to newly created or modified columns. This
        feature is supported in pandas for Python 3.6 and later but not in
        Koalas. In Koalas, all items are computed first, and then assigned.
        """
        from databricks.koalas.series import Series
        for k, v in kwargs.items():
            if not (isinstance(v, (Series, spark.Column)) or
                    callable(v) or pd.api.types.is_scalar(v)):
                raise TypeError("Column assignment doesn't support type "
                                "{0}".format(type(v).__name__))
            if callable(v):
                kwargs[k] = v(self)

        pairs = list(kwargs.items())
        sdf = self._sdf
        for (name, c) in pairs:
            if isinstance(c, Series):
                sdf = sdf.withColumn(name, c._scol)
            elif isinstance(c, Column):
                sdf = sdf.withColumn(name, c)
            else:
                sdf = sdf.withColumn(name, F.lit(c))

        data_columns = self._metadata.data_columns
        metadata = self._metadata.copy(
            data_columns=(data_columns +
                          [name for name, _ in pairs if name not in data_columns]))
        return DataFrame(sdf, metadata)

    def to_excel(self, excel_writer, sheet_name="Sheet1", na_rep="", float_format=None,
                 columns=None, header=True, index=True, index_label=None, startrow=0,
                 startcol=0, engine=None, merge_cells=True, encoding=None, inf_rep="inf",
                 verbose=True, freeze_panes=None):
        """
        Write object to an Excel sheet.

        .. note:: This method should only be used if the resulting DataFrame is expected
                  to be small, as all the data is loaded into the driver's memory.

        To write a single object to an Excel .xlsx file it is only necessary to
        specify a target file name. To write to multiple sheets it is necessary to
        create an `ExcelWriter` object with a target file name, and specify a sheet
        in the file to write to.

        Multiple sheets may be written to by specifying unique `sheet_name`.
        With all data written to the file it is necessary to save the changes.
        Note that creating an `ExcelWriter` object with a file name that already
        exists will result in the contents of the existing file being erased.

        Parameters
        ----------
        excel_writer : str or ExcelWriter object
            File path or existing ExcelWriter.
        sheet_name : str, default 'Sheet1'
            Name of sheet which will contain DataFrame.
        na_rep : str, default ''
            Missing data representation.
        float_format : str, optional
            Format string for floating point numbers. For example
            ``float_format="%%.2f"`` will format 0.1234 to 0.12.
        columns : sequence or list of str, optional
            Columns to write.
        header : bool or list of str, default True
            Write out the column names. If a list of string is given it is
            assumed to be aliases for the column names.
        index : bool, default True
            Write row names (index).
        index_label : str or sequence, optional
            Column label for index column(s) if desired. If not specified, and
            `header` and `index` are True, then the index names are used. A
            sequence should be given if the DataFrame uses MultiIndex.
        startrow : int, default 0
            Upper left cell row to dump data frame.
        startcol : int, default 0
            Upper left cell column to dump data frame.
        engine : str, optional
            Write engine to use, 'openpyxl' or 'xlsxwriter'. You can also set this
            via the options ``io.excel.xlsx.writer``, ``io.excel.xls.writer``, and
            ``io.excel.xlsm.writer``.
        merge_cells : bool, default True
            Write MultiIndex and Hierarchical Rows as merged cells.
        encoding : str, optional
            Encoding of the resulting excel file. Only necessary for xlwt,
            other writers support unicode natively.
        inf_rep : str, default 'inf'
            Representation for infinity (there is no native representation for
            infinity in Excel).
        verbose : bool, default True
            Display more information in the error logs.
        freeze_panes : tuple of int (length 2), optional
            Specifies the one-based bottommost row and rightmost column that
            is to be frozen.

        Notes
        -----
        Once a workbook has been saved it is not possible write further data
        without rewriting the whole workbook.

        Examples
        --------
        Create, write to and save a workbook:

        >>> df1 = ks.DataFrame([['a', 'b'], ['c', 'd']],
        ...                    index=['row 1', 'row 2'],
        ...                    columns=['col 1', 'col 2'])
        >>> df1.to_excel("output.xlsx")  # doctest: +SKIP

        To specify the sheet name:

        >>> df1.to_excel("output.xlsx")  # doctest: +SKIP
        >>> df1.to_excel("output.xlsx",
        ...              sheet_name='Sheet_name_1')  # doctest: +SKIP

        If you wish to write to more than one sheet in the workbook, it is
        necessary to specify an ExcelWriter object:

        >>> with pd.ExcelWriter('output.xlsx') as writer:  # doctest: +SKIP
        ...      df1.to_excel(writer, sheet_name='Sheet_name_1')
        ...      df2.to_excel(writer, sheet_name='Sheet_name_2')

        To set the library that is used to write the Excel file,
        you can pass the `engine` keyword (the default engine is
        automatically chosen depending on the file extension):

        >>> df1.to_excel('output1.xlsx', engine='xlsxwriter')  # doctest: +SKIP
        """

        # Make sure locals() call is at the top of the function so we don't capture local variables.
        args = locals()
        kdf = self

        return validate_arguments_and_invoke_function(
            kdf.to_pandas(), self.to_excel, pd.DataFrame.to_excel, args)

    @property
    def loc(self):
        return SparkDataFrameLocator(self)

    def copy(self):
        return DataFrame(self._sdf, self._metadata.copy())

    def dropna(self, axis=0, how='any', thresh=None, subset=None, inplace=False):
        """
        Remove missing values.

        Parameters
        ----------
        axis : {0 or 'index'}, default 0
            Determine if rows or columns which contain missing values are
            removed.

            * 0, or 'index' : Drop rows which contain missing values.
        how : {'any', 'all'}, default 'any'
            Determine if row or column is removed from DataFrame, when we have
            at least one NA or all NA.

            * 'any' : If any NA values are present, drop that row or column.
            * 'all' : If all values are NA, drop that row or column.

        thresh : int, optional
            Require that many non-NA values.
        subset : array-like, optional
            Labels along other axis to consider, e.g. if you are dropping rows
            these would be a list of columns to include.
        inplace : bool, default False
            If True, do operation inplace and return None.

        Returns
        -------
        DataFrame
            DataFrame with NA entries dropped from it.

        See Also
        --------
        DataFrame.drop : Drop specified labels from columns.
        DataFrame.isnull: Indicate missing values.
        DataFrame.notnull : Indicate existing (non-missing) values.

        Examples
        --------
        >>> df = ks.DataFrame({"name": ['Alfred', 'Batman', 'Catwoman'],
        ...                    "toy": [None, 'Batmobile', 'Bullwhip'],
        ...                    "born": [None, "1940-04-25", None]},
        ...                   columns=['name', 'toy', 'born'])
        >>> df
               name        toy        born
        0    Alfred       None        None
        1    Batman  Batmobile  1940-04-25
        2  Catwoman   Bullwhip        None

        Drop the rows where at least one element is missing.

        >>> df.dropna()
             name        toy        born
        1  Batman  Batmobile  1940-04-25

        Drop the rows where all elements are missing.

        >>> df.dropna(how='all')
               name        toy        born
        0    Alfred       None        None
        1    Batman  Batmobile  1940-04-25
        2  Catwoman   Bullwhip        None

        Keep only the rows with at least 2 non-NA values.

        >>> df.dropna(thresh=2)
               name        toy        born
        1    Batman  Batmobile  1940-04-25
        2  Catwoman   Bullwhip        None

        Define in which columns to look for missing values.

        >>> df.dropna(subset=['name', 'born'])
             name        toy        born
        1  Batman  Batmobile  1940-04-25

        Keep the DataFrame with valid entries in the same variable.

        >>> df.dropna(inplace=True)
        >>> df
             name        toy        born
        1  Batman  Batmobile  1940-04-25
        """
        if axis == 0 or axis == 'index':
            if subset is not None:
                if isinstance(subset, str):
                    columns = [subset]
                else:
                    columns = list(subset)
                invalids = [column for column in columns
                            if column not in self._metadata.data_columns]
                if len(invalids) > 0:
                    raise KeyError(invalids)
            else:
                columns = list(self.columns)

            cnt = reduce(lambda x, y: x + y,
                         [F.when(self[column].notna()._scol, 1).otherwise(0)
                          for column in columns],
                         F.lit(0))
            if thresh is not None:
                pred = cnt >= F.lit(int(thresh))
            elif how == 'any':
                pred = cnt == F.lit(len(columns))
            elif how == 'all':
                pred = cnt > F.lit(0)
            else:
                if how is not None:
                    raise ValueError('invalid how option: {h}'.format(h=how))
                else:
                    raise TypeError('must specify how or thresh')

            sdf = self._sdf.filter(pred)
            if inplace:
                self._sdf = sdf
            else:
                return DataFrame(sdf, self._metadata.copy())

        else:
            raise NotImplementedError("dropna currently only works for axis=0 or axis='index'")

    def fillna(self, value=None, axis=None, inplace=False):
        """Fill NA/NaN values.

        :param value: scalar, dict, Series
                    Value to use to fill holes. alternately a dict/Series of values
                    specifying which value to use for each column.
                    DataFrame is not supported.
        :param axis: {0 or `index`}
                    1 and `columns` are not supported.
        :param inplace: boolean, default False
                    Fill in place (do not create a new object)
        :return: :class:`DataFrame`

        Examples
        --------
        >>> df = ks.DataFrame({
        ...     'A': [None, 3, None, None],
        ...     'B': [2, 4, None, 3],
        ...     'C': [None, None, None, 1],
        ...     'D': [0, 1, 5, 4]
        ...     },
        ...     columns=['A', 'B', 'C', 'D'])
        >>> df
             A    B    C  D
        0  NaN  2.0  NaN  0
        1  3.0  4.0  NaN  1
        2  NaN  NaN  NaN  5
        3  NaN  3.0  1.0  4

        Replace all NaN elements with 0s.

        >>> df.fillna(0)
             A    B    C  D
        0  0.0  2.0  0.0  0
        1  3.0  4.0  0.0  1
        2  0.0  0.0  0.0  5
        3  0.0  3.0  1.0  4

        Replace all NaN elements in column 'A', 'B', 'C', and 'D', with 0, 1,
        2, and 3 respectively.

        >>> values = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
        >>> df.fillna(value=values)
             A    B    C  D
        0  0.0  2.0  2.0  0
        1  3.0  4.0  2.0  1
        2  0.0  1.0  2.0  5
        3  0.0  3.0  1.0  4
        """
        if axis is None:
            axis = 0
        if not (axis == 0 or axis == "index"):
            raise NotImplementedError("fillna currently only works for axis=0 or axis='index'")

        if value is None:
            raise ValueError('Currently must specify value')
        if not isinstance(value, (float, int, str, bool, dict, pd.Series)):
            raise TypeError("Unsupported type %s" % type(value))
        if isinstance(value, pd.Series):
            value = value.to_dict()
        if isinstance(value, dict):
            for v in value.values():
                if not isinstance(v, (float, int, str, bool)):
                    raise TypeError("Unsupported type %s" % type(v))

        sdf = self._sdf.fillna(value)
        if inplace:
            self._sdf = sdf
        else:
            return DataFrame(sdf, self._metadata.copy())

    def head(self, n=5):
        """
        Return the first `n` rows.

        This function returns the first `n` rows for the object based
        on position. It is useful for quickly testing if your object
        has the right type of data in it.

        Parameters
        ----------
        n : int, default 5
            Number of rows to select.

        Returns
        -------
        obj_head : same type as caller
            The first `n` rows of the caller object.

        Examples
        --------
        >>> df = ks.DataFrame({'animal':['alligator', 'bee', 'falcon', 'lion',
        ...                    'monkey', 'parrot', 'shark', 'whale', 'zebra']})
        >>> df
              animal
        0  alligator
        1        bee
        2     falcon
        3       lion
        4     monkey
        5     parrot
        6      shark
        7      whale
        8      zebra

        Viewing the first 5 lines

        >>> df.head()
              animal
        0  alligator
        1        bee
        2     falcon
        3       lion
        4     monkey

        Viewing the first `n` lines (three in this case)

        >>> df.head(3)
              animal
        0  alligator
        1        bee
        2     falcon
        """

        return DataFrame(self._sdf.limit(n), self._metadata.copy())

    @property
    def columns(self):
        """The column labels of the DataFrame."""
        return pd.Index(self._metadata.data_columns)

    @columns.setter
    def columns(self, names):
        old_names = self._metadata.data_columns
        if len(old_names) != len(names):
            raise ValueError(
                "Length mismatch: Expected axis has %d elements, new values have %d elements"
                % (len(old_names), len(names)))
        sdf = self._sdf.select(self._metadata.index_columns +
                               [self[old_name]._scol.alias(new_name)
                                for (old_name, new_name) in zip(old_names, names)])
        self._sdf = sdf
        self._metadata = self._metadata.copy(data_columns=names)

    @property
    def dtypes(self):
        """Return the dtypes in the DataFrame.

        This returns a Series with the data type of each column. The result's index is the original
        DataFrame's columns. Columns with mixed types are stored with the object dtype.

        :return: :class:`pd.Series` The data type of each column.

        Examples
        --------
        >>> df = ks.DataFrame({'a': list('abc'),
        ...                    'b': list(range(1, 4)),
        ...                    'c': np.arange(3, 6).astype('i1'),
        ...                    'd': np.arange(4.0, 7.0, dtype='float64'),
        ...                    'e': [True, False, True],
        ...                    'f': pd.date_range('20130101', periods=3)},
        ...                   columns=['a', 'b', 'c', 'd', 'e', 'f'])
        >>> df.dtypes
        a            object
        b             int64
        c              int8
        d           float64
        e              bool
        f    datetime64[ns]
        dtype: object
        """
        return pd.Series([self[col].dtype for col in self._metadata.data_columns],
                         index=self._metadata.data_columns)

    def count(self):
        """
        Count non-NA cells for each column.

        The values `None`, `NaN` are considered NA.

        Returns
        -------
        pandas.Series

        See Also
        --------
        Series.count: Number of non-NA elements in a Series.
        DataFrame.shape: Number of DataFrame rows and columns (including NA
            elements).
        DataFrame.isna: Boolean same-sized DataFrame showing places of NA
            elements.

        Examples
        --------
        Constructing DataFrame from a dictionary:

        >>> df = ks.DataFrame({"Person":
        ...                    ["John", "Myla", "Lewis", "John", "Myla"],
        ...                    "Age": [24., np.nan, 21., 33, 26],
        ...                    "Single": [False, True, True, True, False]},
        ...                   columns=["Person", "Age", "Single"])
        >>> df
          Person   Age  Single
        0   John  24.0   False
        1   Myla   NaN    True
        2  Lewis  21.0    True
        3   John  33.0    True
        4   Myla  26.0   False

        Notice the uncounted NA values:

        >>> df.count()
        Person    5
        Age       4
        Single    5
        dtype: int64
        """
        return self._reduce_for_stat_function(_Frame._count_expr)

    def drop(self, labels=None, axis=1, columns: Union[str, List[str]] = None):
        """
        Drop specified labels from columns.

        Remove columns by specifying label names and axis=1 or columns.
        When specifying both labels and columns, only labels will be dropped.
        Removing rows is yet to be implemented.

        Parameters
        ----------
        labels : single label or list-like
            Column labels to drop.
        axis : {1 or 'columns'}, default 1
            .. dropna currently only works for axis=1 'columns'
               axis=0 is yet to be implemented.
        columns : single label or list-like
            Alternative to specifying axis (``labels, axis=1``
            is equivalent to ``columns=labels``).

        Returns
        -------
        dropped : DataFrame

        See Also
        --------
        Series.dropna

        Examples
        --------
        >>> df = ks.DataFrame({'x': [1, 2], 'y': [3, 4], 'z': [5, 6], 'w': [7, 8]},
        ...                   columns=['x', 'y', 'z', 'w'])
        >>> df
           x  y  z  w
        0  1  3  5  7
        1  2  4  6  8

        >>> df.drop('x', axis=1)
           y  z  w
        0  3  5  7
        1  4  6  8

        >>> df.drop(['y', 'z'], axis=1)
           x  w
        0  1  7
        1  2  8

        >>> df.drop(columns=['y', 'z'])
           x  w
        0  1  7
        1  2  8

        Notes
        -----
        Currently only axis = 1 is supported in this function,
        axis = 0 is yet to be implemented.
        """
        if labels is not None:
            axis = self._validate_axis(axis)
            if axis == 1:
                return self.drop(columns=labels)
            raise NotImplementedError("Drop currently only works for axis=1")
        elif columns is not None:
            if isinstance(columns, str):
                columns = [columns]
            sdf = self._sdf.drop(*columns)
            metadata = self._metadata.copy(
                data_columns=[column for column in self.columns if column not in columns]
            )
            return DataFrame(sdf, metadata)
        else:
            raise ValueError("Need to specify at least one of 'labels' or 'columns'")

    def get(self, key, default=None):
        """
        Get item from object for given key (DataFrame column, Panel slice,
        etc.). Returns default value if not found.

        Parameters
        ----------
        key : object

        Returns
        -------
        value : same type as items contained in object

        Examples
        --------
        >>> df = ks.DataFrame({'x':range(3), 'y':['a','b','b'], 'z':['a','b','b']},
        ...                   columns=['x', 'y', 'z'])
        >>> df
           x  y  z
        0  0  a  a
        1  1  b  b
        2  2  b  b

        >>> df.get('x')
        0    0
        1    1
        2    2
        Name: x, dtype: int64

        >>> df.get(['x', 'y'])
           x  y
        0  0  a
        1  1  b
        2  2  b
        """
        try:
            return self._pd_getitem(key)
        except (KeyError, ValueError, IndexError):
            return default

    def sort_values(self, by, ascending=True, inplace=False, na_position='last'):
        """
        Sort by the values along either axis.

        Parameters
        ----------
        by : str or list of str
        ascending : bool or list of bool, default True
             Sort ascending vs. descending. Specify list for multiple sort
             orders.  If this is a list of bools, must match the length of
             the by.
        inplace : bool, default False
             if True, perform operation in-place
        na_position : {'first', 'last'}, default 'last'
             `first` puts NaNs at the beginning, `last` puts NaNs at the end

        Returns
        -------
        sorted_obj : DataFrame

        Examples
        --------
        >>> df = ks.DataFrame({
        ...     'col1': ['A', 'B', None, 'D', 'C'],
        ...     'col2': [2, 9, 8, 7, 4],
        ...     'col3': [0, 9, 4, 2, 3],
        ...   },
        ...   columns=['col1', 'col2', 'col3'])
        >>> df
           col1  col2  col3
        0     A     2     0
        1     B     9     9
        2  None     8     4
        3     D     7     2
        4     C     4     3

        Sort by col1

        >>> df.sort_values(by=['col1'])
           col1  col2  col3
        0     A     2     0
        1     B     9     9
        4     C     4     3
        3     D     7     2
        2  None     8     4

        Sort Descending

        >>> df.sort_values(by='col1', ascending=False)
           col1  col2  col3
        3     D     7     2
        4     C     4     3
        1     B     9     9
        0     A     2     0
        2  None     8     4

        Sort by multiple columns

        >>> df = ks.DataFrame({
        ...     'col1': ['A', 'A', 'B', None, 'D', 'C'],
        ...     'col2': [2, 1, 9, 8, 7, 4],
        ...     'col3': [0, 1, 9, 4, 2, 3],
        ...   },
        ...   columns=['col1', 'col2', 'col3'])
        >>> df.sort_values(by=['col1', 'col2'])
           col1  col2  col3
        1     A     1     1
        0     A     2     0
        2     B     9     9
        5     C     4     3
        4     D     7     2
        3  None     8     4
        """
        if isinstance(by, str):
            by = [by]
        if isinstance(ascending, bool):
            ascending = [ascending] * len(by)
        if len(ascending) != len(by):
            raise ValueError('Length of ascending ({}) != length of by ({})'
                             .format(len(ascending), len(by)))
        if na_position not in ('first', 'last'):
            raise ValueError("invalid na_position: '{}'".format(na_position))

        # Mapper: Get a spark column function for (ascending, na_position) combination
        # Note that 'asc_nulls_first' and friends were added as of Spark 2.4, see SPARK-23847.
        mapper = {
            (True, 'first'): lambda x: Column(getattr(x._jc, "asc_nulls_first")()),
            (True, 'last'): lambda x: Column(getattr(x._jc, "asc_nulls_last")()),
            (False, 'first'): lambda x: Column(getattr(x._jc, "desc_nulls_first")()),
            (False, 'last'): lambda x: Column(getattr(x._jc, "desc_nulls_last")()),
        }
        by = [mapper[(asc, na_position)](self[colname]._scol)
              for colname, asc in zip(by, ascending)]
        kdf = DataFrame(self._sdf.sort(*by), self._metadata.copy())
        if inplace:
            self._sdf = kdf._sdf
            self._metadata = kdf._metadata
        else:
            return kdf

    def isin(self, values):
        """
        Whether each element in the DataFrame is contained in values.

        Parameters
        ----------
        values : iterable or dict
           The sequence of values to test. If values is a dict,
           the keys must be the column names, which must match.
           Series and DataFrame are not supported.
        Returns
        -------
        DataFrame
            DataFrame of booleans showing whether each element in the DataFrame
            is contained in values.

        Examples
        --------
        >>> df = ks.DataFrame({'num_legs': [2, 4], 'num_wings': [2, 0]},
        ...                   index=['falcon', 'dog'],
        ...                   columns=['num_legs', 'num_wings'])
        >>> df
                num_legs  num_wings
        falcon         2          2
        dog            4          0

        When ``values`` is a list check whether every value in the DataFrame
        is present in the list (which animals have 0 or 2 legs or wings)

        >>> df.isin([0, 2])
                num_legs  num_wings
        falcon      True       True
        dog        False       True

        When ``values`` is a dict, we can pass values to check for each
        column separately:

        >>> df.isin({'num_wings': [0, 3]})
                num_legs  num_wings
        falcon     False      False
        dog        False       True
        """
        if isinstance(values, (pd.DataFrame, pd.Series)):
            raise NotImplementedError("DataFrame and Series are not supported")
        if isinstance(values, dict) and not set(values.keys()).issubset(self.columns):
            raise AttributeError(
                "'DataFrame' object has no attribute %s"
                % (set(values.keys()).difference(self.columns)))

        _select_columns = self._metadata.index_columns
        if isinstance(values, dict):
            for col in self.columns:
                if col in values:
                    _select_columns.append(self[col]._scol.isin(values[col]).alias(col))
                else:
                    _select_columns.append(F.lit(False).alias(col))
        elif is_list_like(values):
            _select_columns += [
                self[col]._scol.isin(list(values)).alias(col) for col in self.columns]
        else:
            raise TypeError('Values should be iterable, Series, DataFrame or dict.')

        return DataFrame(self._sdf.select(_select_columns), self._metadata.copy())

    def pipe(self, func, *args, **kwargs):
        r"""
        Apply func(self, \*args, \*\*kwargs).

        Parameters
        ----------
        func : function
            function to apply to the DataFrame.
            ``args``, and ``kwargs`` are passed into ``func``.
            Alternatively a ``(callable, data_keyword)`` tuple where
            ``data_keyword`` is a string indicating the keyword of
            ``callable`` that expects the DataFrames.
        args : iterable, optional
            positional arguments passed into ``func``.
        kwargs : mapping, optional
            a dictionary of keyword arguments passed into ``func``.

        Returns
        -------
        object : the return type of ``func``.

        Notes
        -----
        Use ``.pipe`` when chaining together functions that expect
        Series, DataFrames or GroupBy objects. For example, given

        >>> df = ks.DataFrame({'category': ['A', 'A', 'B'],
        ...                    'col1': [1, 2, 3],
        ...                    'col2': [4, 5, 6]},
        ...                   columns=['category', 'col1', 'col2'])
        >>> def keep_category_a(df):
        ...    return df[df['category'] == 'A']
        >>> def add_one(df, column):
        ...    return df.assign(col3=df[column] + 1)
        >>> def multiply(df, column1, column2):
        ...    return df.assign(col4=df[column1] * df[column2])


        instead of writing

        >>> multiply(add_one(keep_category_a(df), column="col1"), column1="col2", column2="col3")
          category  col1  col2  col3  col4
        0        A     1     4     2     8
        1        A     2     5     3    15


        You can write

        >>> (df.pipe(keep_category_a)
        ...    .pipe(add_one, column="col1")
        ...    .pipe(multiply, column1="col2", column2="col3")
        ... )
          category  col1  col2  col3  col4
        0        A     1     4     2     8
        1        A     2     5     3    15


        If you have a function that takes the data as (say) the second
        argument, pass a tuple indicating which keyword expects the
        data. For example, suppose ``f`` takes its data as ``df``:

        >>> def multiply_2(column1, df, column2):
        ...     return df.assign(col4=df[column1] * df[column2])


        Then you can write

        >>> (df.pipe(keep_category_a)
        ...    .pipe(add_one, column="col1")
        ...    .pipe((multiply_2, 'df'), column1="col2", column2="col3")
        ... )
          category  col1  col2  col3  col4
        0        A     1     4     2     8
        1        A     2     5     3    15
        """

        if isinstance(func, tuple):
            func, target = func
            if target in kwargs:
                raise ValueError('%s is both the pipe target and a keyword '
                                 'argument' % target)
            kwargs[target] = self
            return func(*args, **kwargs)
        else:
            return func(self, *args, **kwargs)

    @property
    def shape(self):
        """
        Return a tuple representing the dimensionality of the DataFrame.

        Examples
        --------
        >>> df = ks.DataFrame({'col1': [1, 2], 'col2': [3, 4]})
        >>> df.shape
        (2, 2)

        >>> df = ks.DataFrame({'col1': [1, 2], 'col2': [3, 4],
        ...                    'col3': [5, 6]})
        >>> df.shape
        (2, 3)
        """
        return len(self), len(self.columns)

    def merge(self, right: 'DataFrame', how: str = 'inner', on: str = None,
              left_index: bool = False, right_index: bool = False,
              suffixes: Tuple[str, str] = ('_x', '_y')) -> 'DataFrame':
        """
        Merge DataFrame objects with a database-style join.

        Parameters
        ----------
        right: Object to merge with.
        how: Type of merge to be performed.
            {‘left’, ‘right’, ‘outer’, ‘inner’}, default ‘inner’

            left: use only keys from left frame, similar to a SQL left outer join; preserve key
                order.
            right: use only keys from right frame, similar to a SQL right outer join; preserve key
                order.
            outer: use union of keys from both frames, similar to a SQL full outer join; sort keys
                lexicographically.
            inner: use intersection of keys from both frames, similar to a SQL inner join;
                preserve the order of the left keys.
        on: Column or index level names to join on. These must be found in both DataFrames. If on
            is None and not merging on indexes then this defaults to the intersection of the
            columns in both DataFrames.
        left_index: Use the index from the left DataFrame as the join key(s). If it is a
            MultiIndex, the number of keys in the other DataFrame (either the index or a number of
            columns) must match the number of levels.
        right_index: Use the index from the right DataFrame as the join key. Same caveats as
            left_index.
        suffixes: Suffix to apply to overlapping column names in the left and right side,
            respectively.

        Returns
        -------
        DataFrame
            A DataFrame of the two merged objects.

        Examples
        --------
        >>> left_kdf = ks.DataFrame({'A': [1, 2]})
        >>> right_kdf = ks.DataFrame({'B': ['x', 'y']}, index=[1, 2])

        >>> left_kdf.merge(right_kdf, left_index=True, right_index=True)
           A  B
        0  2  x

        >>> left_kdf.merge(right_kdf, left_index=True, right_index=True, how='left')
           A     B
        0  1  None
        1  2     x

        >>> left_kdf.merge(right_kdf, left_index=True, right_index=True, how='right')
             A  B
        0  2.0  x
        1  NaN  y

        >>> left_kdf.merge(right_kdf, left_index=True, right_index=True, how='outer')
             A     B
        0  1.0  None
        1  2.0     x
        2  NaN     y

        Notes
        -----
        As described in #263, joining string columns currently returns None for missing values
            instead of NaN.
        """
        if on is None and not left_index and not right_index:
            raise SparkPandasMergeError("At least 'on' or 'left_index' and 'right_index' have ",
                                        "to be set")
        if on is not None and (left_index or right_index):
            raise SparkPandasMergeError("Only 'on' or 'left_index' and 'right_index' can be set")

        if how == 'full':
            warnings.warn("Warning: While Koalas will accept 'full', you should use 'outer' " +
                          "instead to be compatible with the pandas merge API", UserWarning)
        if how == 'outer':
            # 'outer' in pandas equals 'full' in Spark
            how = 'full'
        if how not in ('inner', 'left', 'right', 'full'):
            raise ValueError("The 'how' parameter has to be amongst the following values: ",
                             "['inner', 'left', 'right', 'outer']")

        if on is None:
            # FIXME Move index string to constant?
            on = '__index_level_0__'

        left_table = self._sdf.alias('left_table')
        right_table = right._sdf.alias('right_table')

        # Unpack suffixes tuple for convenience
        left_suffix = suffixes[0]
        right_suffix = suffixes[1]

        # Append suffixes to columns with the same name to avoid conflicts later
        duplicate_columns = list(self.columns & right.columns)
        if duplicate_columns:
            for duplicate_column_name in duplicate_columns:
                left_table = left_table.withColumnRenamed(duplicate_column_name,
                                                          duplicate_column_name + left_suffix)
                right_table = right_table.withColumnRenamed(duplicate_column_name,
                                                            duplicate_column_name + right_suffix)

        join_condition = (left_table[on] == right_table[on] if on not in duplicate_columns
                          else left_table[on + left_suffix] == right_table[on + right_suffix])
        joined_table = left_table.join(right_table, join_condition, how=how)

        if on in duplicate_columns:
            # Merge duplicate key columns
            joined_table = joined_table.withColumnRenamed(on + left_suffix, on)
            joined_table = joined_table.drop(on + right_suffix)

        # Remove auxiliary index
        # FIXME Move index string to constant?
        joined_table = joined_table.drop('__index_level_0__')

        kdf = DataFrame(joined_table)
        return kdf

    def _pd_getitem(self, key):
        from databricks.koalas.series import Series
        if key is None:
            raise KeyError("none key")
        if isinstance(key, str):
            try:
                return Series(self._sdf.__getitem__(key), anchor=self,
                              index=self._metadata.index_map)
            except AnalysisException:
                raise KeyError(key)
        if np.isscalar(key) or isinstance(key, (tuple, str)):
            raise NotImplementedError(key)
        elif isinstance(key, slice):
            return self.loc[key]

        if isinstance(key, (pd.Series, np.ndarray, pd.Index)):
            raise NotImplementedError(key)
        if isinstance(key, list):
            return self.loc[:, key]
        if isinstance(key, DataFrame):
            # TODO Should not implement alignment, too dangerous?
            return Series(self._sdf.__getitem__(key), anchor=self, index=self._metadata.index_map)
        if isinstance(key, Series):
            # TODO Should not implement alignment, too dangerous?
            # It is assumed to be only a filter, otherwise .loc should be used.
            bcol = key._scol.cast("boolean")
            return DataFrame(self._sdf.filter(bcol), self._metadata.copy())
        raise NotImplementedError(key)

    def __repr__(self):
        return repr(self.head(max_display_count).to_pandas())

    def _repr_html_(self):
        return self.head(max_display_count).to_pandas()._repr_html_()

    def __getitem__(self, key):
        return self._pd_getitem(key)

    def __setitem__(self, key, value):
        from databricks.koalas.series import Series
        # For now, we don't support realignment against different dataframes.
        # This is too expensive in Spark.
        # Are we assigning against a column?
        if isinstance(value, Series):
            assert value._kdf is self, \
                "Cannot combine column argument because it comes from a different dataframe"
        if isinstance(key, (tuple, list)):
            assert isinstance(value.schema, StructType)
            field_names = value.schema.fieldNames()
            kdf = self.assign(**{k: value[c] for k, c in zip(key, field_names)})
        else:
            kdf = self.assign(**{key: value})

        self._sdf = kdf._sdf
        self._metadata = kdf._metadata

    def __getattr__(self, key: str) -> Any:
        from databricks.koalas.series import Series
        if key.startswith("__") or key.startswith("_pandas_") or key.startswith("_spark_"):
            raise AttributeError(key)
        if hasattr(_MissingPandasLikeDataFrame, key):
            property_or_func = getattr(_MissingPandasLikeDataFrame, key)
            if isinstance(property_or_func, property):
                return property_or_func.fget(self)  # type: ignore
            else:
                return partial(property_or_func, self)
        return Series(self._sdf.__getattr__(key), anchor=self, index=self._metadata.index_map)

    def __iter__(self):
        return self.toPandas().__iter__()

    def __len__(self):
        return self._sdf.count()

    def __dir__(self):
        fields = [f for f in self._sdf.schema.fieldNames() if ' ' not in f]
        return super(DataFrame, self).__dir__() + fields

    @classmethod
    def _validate_axis(cls, axis=0):
        if axis not in (0, 1, 'index', 'columns', None):
            raise ValueError('No axis named {0}'.format(axis))
        # convert to numeric axis
        return {None: 0, 'index': 0, 'columns': 1}.get(axis, axis)


def _reduce_spark_multi(sdf, aggs):
    """
    Performs a reduction on a dataframe, the functions being known sql aggregate functions.
    """
    assert isinstance(sdf, spark.DataFrame)
    sdf0 = sdf.agg(*aggs)
    l = sdf0.head(2)
    assert len(l) == 1, (sdf, l)
    row = l[0]
    l2 = list(row)
    assert len(l2) == len(aggs), (row, l2)
    return l2
