# encoding: utf8
# This module handles evaluation of external atoms via plugins for the Clingo backend.

# HEXLite Python-based solver for a fragment of HEX
# Copyright (C) 2017  Peter Schueller <schueller.p@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import sys
import logging
import collections
import itertools
import pprint
import traceback

import dlvhex

from . import common as hexlite
from . import aux
from .ast import shallowparser as shp
from . import explicitflpcheck as flp

# assume that the main program has handled possible import problems
import clingo

class ClaspContext:
  '''
  context within the propagator
  * clasp context with PropagateControl object
  * ClingoPropagator object that contains, e.g., propagation init symbol information
  '''
  def __init__(self):
    self.propcontrol = None
    self.propagator = None
  def __call__(self, control, propagator):
    '''
    initialize context with control object
    '''
    assert(self.propcontrol == None)
    assert(control != None)
    assert(isinstance(control, clingo.PropagateControl))
    self.propcontrol = control
    assert(isinstance(propagator, ClingoPropagator))
    self.propagator = propagator
    return self
  def __enter__(self):
    pass
  def __exit__(self, type, value, traceback):
    self.propcontrol = None
    self.propagator = None

class SymLit:
  '''
  Holds the symbol of a symbolic atom of Clingo plus its solver literal.

  x <- init.symbolic_atoms.by_signature
  sym <- x.symbol
  lit <- init.solver_literal(x.literal)

  if lit is None, then
  * sym is a term and not an atom and there is no solver literal
  * sym is used as a non-predicate-input to an external atom (TODO ensure this is always true)
  * TODO document other cases
  '''
  def __init__(self, sym, lit):
    self.sym = sym
    #if lit is None:
    #  logging.warning("SYMLIT {} with empty LIT from {}".format(sym, '\n'.join(traceback.format_stack())))
    self.lit = lit

  def __hash__(self):
    return hash(self.sym)

class ClingoID:
  # the ID class as passed to plugins, from view of Clingo backend
  def __init__(self, ccontext, symlit):
    assert(isinstance(ccontext, ClaspContext))
    self.ccontext = ccontext
    self.symlit = symlit
    self.__value = str(symlit.sym)

  def negate(self):
    return ClingoID(self.ccontext, SymLit(self.symlit.sym, -self.symlit.lit))

  def value(self):
    return self.__value

  def intValue(self):
    if self.symlit.sym.type == clingo.SymbolType.Number:
      return self.symlit.sym.number
    else:
      raise Exception('intValue called on ID {} which is not a number!'.format(self.__value))

  def isTrue(self):
    if not self.symlit.lit:
      raise Exception("cannot call isTrue on term that is not an atom")
    return self.__assignment().is_true(self.symlit.lit)

  def isFalse(self):
    if not self.symlit.lit:
      raise Exception("cannot call isFalse on term that is not an atom")
    return self.__assignment().is_false(self.symlit.lit)

  def isAssigned(self):
    if not self.symlit.lit:
      raise Exception("cannot call isAssigned on term that is not an atom")
    return self.__assignment().value(self.symlit.lit) != None

  def tuple(self):
    tup = tuple([ ClingoID(self.ccontext, SymLit(sym, None)) for sym in
                  [clingo.Function(self.symlit.sym.name)]+self.symlit.sym.arguments])
    return tup

  def extension(self):
    '''
    returns a sequence of tuples of true atoms with predicate using this ClingoID
    fails if this ClingoID does not hold a constant
    '''
    if self.symlit.sym.type != clingo.SymbolType.Function or self.symlit.sym.arguments != []:
      raise Exception("cannot call extension() on term that is not a constant. was called on {}".format(self.__value))
    # extract all true atoms with matching predicate name
    ret_atoms = [
      x for x in dlvhex.getTrueInputAtoms()
      if x.symlit.sym.type == clingo.SymbolType.Function and x.symlit.sym.name == self.__value ]
    # convert into tuples of ClingoIDs without literal (they are terms, not atoms)
    ret = frozenset([
      tuple([ClingoID(self.ccontext, SymLit(term, None)) for term in x.symlit.sym.arguments])
      for x in ret_atoms ])
    #logging.warning("extension of {} returned {}".format(self.__value, repr(ret)))
    return ret

  def __assignment(self):
    return self.ccontext.propcontrol.assignment

  def __str__(self):
    return self.__value

  def __repr__(self):
    sign = ''
    if self.symlit.lit and self.symlit.lit < 0:
      sign = '-'
    return "{}ClingoID({})".format(sign, str(self))

  def __hash__(self):
    return hash(self.symlit)

  def __eq__(self, other):
    if isinstance(other, str):
      return self.value() == other
    elif isinstance(other, int) and self.symlit.sym.type == clingo.SymbolType.Number:
      return self.intValue() == other
    elif isinstance(other, ClingoID):
      return self.symlit.sym == other.symlit.sym
    else:
      return self.value() == other

  def __getattr__(self, name):
    raise Exception("not (yet) implemented: ClingoID.{}".format(name))

class EAtomEvaluator(dlvhex.Backend):
  '''
  Clingo-backend-specific evaluation of external atoms implemented in Python
  using the same API as in the dlvhex solver (but fully realized in Python).

  This closely interacts with the dlvhex package.

  This is one object that evaluates all external atoms in the context of a clasp context.
  '''
  def __init__(self, claspcontext):
    assert(isinstance(claspcontext, ClaspContext))
    self.ccontext = claspcontext
    # keep list of learned nogoods so that we do not add the same one twice
    self.learnedNogoods = set()

  def clingo2hex(self, term):
    assert(isinstance(term, clingo.Symbol))
    #logging.debug("convertClingoToHex got {} with type {}".format(repr(term), term.type))
    return ClingoID(self.ccontext, SymLit(term, None))
    #if term.type is clingo.SymbolType.Number:
    #  ret = term.number
    #elif term.type in [clingo.SymbolType.String, clingo.SymbolType.Function]:
    #  ret = str(term)
    #else:
    #  raise Exception("cannot convert clingo term {} of type {} to external atom term!".format(
    #    repr(term), str(term.type)))
    #return ret

  def hex2clingo(self, term):
    if isinstance(term, ClingoID):
      return term.symlit.sym
    elif isinstance(term, str):
      if term[0] == '"':
        ret = clingo.String(term[1:-1])
      else:
        ret = clingo.parse_term(term)
    elif isinstance(term, int):
      ret = clingo.Number(term)
    else:
      raise Exception("cannot convert external atom term {} to clingo term!".format(repr(term)))
    return ret

  def evaluate(self, holder, inputtuple, predicateinputatoms):
    '''
    Convert input tuple (from clingo to dlvhex) and call external atom semantics function.
    Convert output tuple (from dlvhex to clingo).

    * converts input tuple for execution
    * prepares dlvhex.py for execution
    * executes
    * converts output tuples
    * cleans up
    * return result (known true tuples, unknown tuples)
    '''
    # prepare input tuple
    input_arguments = []
    for spec_idx, inp in enumerate(holder.inspec):
      if inp in [dlvhex.PREDICATE, dlvhex.CONSTANT]:
        arg = self.clingo2hex(inputtuple[spec_idx])
        input_arguments.append(arg)
      elif inp == dlvhex.TUPLE:
        if (spec_idx + 1) != len(holder.inspec):
          raise Exception("got TUPLE type which is not in final argument position")
        # give all remaining arguments as one tuple
        args = [ self.clingo2hex(x) for x in inputtuple[spec_idx:] ]
        input_arguments.append(tuple(args))
      else:
        raise Exception("unknown input type "+repr(inp))

    # call external atom in plugin
    dlvhex.startExternalAtomCall(input_arguments, predicateinputatoms, self, holder)
    outKnownTrue, outUnknown = set(), set()
    try:
      logging.debug('calling plugin eatom with arguments '+repr(input_arguments))
      holder.func(*input_arguments)
      
      # sanity check
      inconsistent = set.intersection(dlvhex.currentEvaluation().outputKnownTrue, dlvhex.currentEvaluation().outputUnknown)
      if len(inconsistent) > 0:
        raise Exception('external atom {} with arguments {} provided the following tuples both as true and unknown: {} partial interpretation is {}'.format(holder.name, repr(input_arguments), repr(inconsistent), repr(predicateinputatoms)))

      # interpret output that is known to be true
      outKnownTrue = [ tuple([ self.hex2clingo(val) for val in _tuple ])
                       for _tuple in dlvhex.currentEvaluation().outputKnownTrue ]

      # interpret output that is unknown whether it is false or true (in partial evaluation)
      outUnknown = [ tuple([ self.hex2clingo(val) for val in _tuple ])
                     for _tuple in dlvhex.currentEvaluation().outputUnknown ]
    finally:
      dlvhex.cleanupExternalAtomCall()
    return outKnownTrue, outUnknown
  
  # implementation of Backend method
  def storeAtom(self, tpl):
    '''
    this can only be called from an external atom code of a user
    it is called after
      dlvhex.startExternalAtomCall(predicateinputatoms, self)
    has been called and the only atoms we can store here are from predicateinputatoms
    (because we do not invent new variables and we cannot access variables that are not about our predicate inputs)

    so we only need to check if tpl is in predicateinputatoms and return the corresponding ClingoID
    '''
    match_name = tpl[0].symlit.sym.name
    match_arguments = [t.symlit.sym for t in tpl[1:]]
    #print("match_name = {} match_arguments = {}".format(repr(match_name), repr(match_arguments)))
    for x in dlvhex.currentEvaluation().input:
      #print("comparing {} with {}".format(repr(x), repr(tpl)))
      #print("xsxn {} xssa {}".format(repr(x.symlit.sym.name), repr(x.symlit.sym.arguments)))
      if x.symlit.sym.name == match_name and x.symlit.sym.arguments == match_arguments:
        #print("found {}".format(repr(x)))
        return x
    logging.warning("storeAtom() called with tuple {} that cannot be stored because it is not part of the predicate input or not existing in the ground rewriting (we have no liberal safety)".format(repr(tpl)))
    return None

  # implementation of Backend method
  def storeOutputAtom(self, args, sign):
    '''
    this can only be called from an external atom code of a user
    it is called after
      dlvhex.startExternalAtomCall(predicateinputatoms, self, holder)
    has been called and the only atoms we can store here are external atom replacement atoms that exist in the theory

    so we only need to assemble the correct tuple and check if it exists in clingo and return the corresponding ClingoID (only literal matters)
    '''
    #logging.debug("got dlvhex.currentEvaluation().holder.name {}".format(dlvhex.currentEvaluation().holder.name))
    #logging.debug("got self.ccontext.propagator.eatomVerifications[dlvhex.currentEvaluation().holder.name] {}".format(repr([ x.replacement.sym for x in self.ccontext.propagator.eatomVerifications[dlvhex.currentEvaluation().holder.name]])))

    match_args = [t.symlit.sym for t in itertools.chain(dlvhex.currentEvaluation().inputTuple, args)]
    #print("looking up {}".format(repr(match_args)))
    # find those verification objects that contain the tuple to be stored
    for x in self.ccontext.propagator.eatomVerifications[dlvhex.currentEvaluation().holder.name]:
      #print("comparing {}".format(repr(x.replacement.sym.arguments)))
      if x.replacement.sym.arguments == match_args:
        #print("for storeOutputAtom({},{}) found replacement {}".format(repr(args), repr(sign), repr(x.replacement)))
        return ClingoID(self.ccontext, x.replacement)
    #  if x.symlit.sym.name == match_name and x.symlit.sym.arguments == match_arguments:
    logging.warning("did not find literal to return in storeOutputAtom for {} will return None".format(repr(args)))
    return None

  # implementation of Backend method
  def learn(self, ng):
    if __debug__:
      logging.debug("learning user-specified nogood "+repr(ng))
    assert(all([isinstance(clingoid, ClingoID) for clingoid in ng]))
    ng = tuple(ng) # make sure it is hashable
    if ng in self.learnedNogoods:
      logging.info("learn() skips adding known nogood")
    else:
      self.learnedNogoods.add(ng)
      nogood = self.ccontext.propagator.Nogood()
      for clingoid in ng:
        if not nogood.add(clingoid.symlit.lit):
          logging.debug("cannot build nogood (opposite literals)!")
          return
      logging.info("learn() adds nogood %s", repr(nogood.literals))
      self.ccontext.propagator.addNogood(nogood)

class CachedEAtomEvaluator(EAtomEvaluator):
  def __init__(self, claspcontext):
    EAtomEvaluator.__init__(self, claspcontext)
    # cache = defaultdict:
    # key = eatom name
    # value = defaultdict
    #   key = inputtuple
    #   value = dict:
    #     key = (predicateinputatoms-true, predicateinputatoms-false)
    #           [because in partial interpretations there are also unknown atoms]
    #     value = output
    self.cache = collections.defaultdict(lambda: collections.defaultdict(dict))

  def evaluateNoncached(self, holder, inputtuple, predicateinputatoms):
    return EAtomEvaluator.evaluate(self, holder, inputtuple, predicateinputatoms)

  def evaluateCached(self, holder, inputtuple, predicateinputatoms):
    # this is handled by defaultdict
    storage = self.cache[holder.name][inputtuple]
    positiveinputatoms = frozenset(x for x in predicateinputatoms if x.isTrue())
    negativeinputatoms = frozenset(x for x in predicateinputatoms if x.isFalse())
    key = (positiveinputatoms, negativeinputatoms)
    if key not in storage:
      storage[key] = EAtomEvaluator.evaluate(
        self, holder, inputtuple, predicateinputatoms)
    return storage[key]

  def evaluate(self, holder, inputtuple, predicateinputatoms):
    # we cache for total and partial evaluations,
    # because our architecture would evaluate multiple times for multiple output tuples
    # of the same nonground external atom on the same (partial) interpretation
    # -> the cache avoids recomputations in this case
    return self.evaluateCached(holder, inputtuple, predicateinputatoms)

class GringoContext:
  class ExternalAtomCall:
    def __init__(self, eaeval, holder):
      self.eaeval = eaeval
      self.holder = holder
    def __call__(self, *arguments):
      logging.debug('GC.EAC(%s) called with %s',self.holder.name, repr(arguments))
      outKnownTrue, outUnknown = self.eaeval.evaluate(self.holder, arguments, [])
      assert(len(outUnknown) == 0) # no partial evaluation for eatoms in grounding
      outarity = self.holder.outnum
      gringoOut = None
      # interpret special cases for gringo @eatom rewritings:
      if outarity == 0:
        # no output arguments: 1 or 0
        if len(outKnownTrue) == 0:
          gringoOut = 0
        else:
          gringoOut = 1
      elif outarity == 1:
        # list of terms, not list of tuples (I could not convince Gringo to process single-element-tuples)
        if any([ len(x) != outarity for x in outKnownTrue ]):
          wrongarity = [ x for x in outKnownTrue if len(x) != outarity ]
          outKnownTrue = [ x for x in outKnownTrue if len(x) == outarity ]
          logging.warning("ignored tuples {} with wrong arity from atom {}".format(repr(wrongarity), self.holder.name))
        gringoOut = [ x[0] for x in outKnownTrue ]
      else:
        gringoOut = outKnownTrue
      # in other cases we can directly use what externalAtomCallHelper returned
      logging.debug('GC.EAC(%s) call returned output %s', self.holder.name, repr(gringoOut))
      return gringoOut
  def __init__(self, eaeval):
    assert(isinstance(eaeval, EAtomEvaluator))
    self.eaeval = eaeval
  def __getattr__(self, attr):
    #logging.debug('GC.%s called',attr)
    return self.ExternalAtomCall(self.eaeval, dlvhex.eatoms[attr])


class ClingoPropagator:

  class EAtomVerification:
    """
    stores everything required to evaluate truth of one ground external atom in propagation:
    * relevance atom (do we need to evaluate it?)
    * replacement atom (was it guessed true or false? which arguments does it have?)
    * the full list of atoms relevant as predicate inputs (required to evaluate the external atom semantic function)
    * whether we should verify this on partial assignments (or only on total ones)
    """
    def __init__(self, relevance, replacement, verify_on_partial=False):
      # symlit for ground eatom relevance
      self.relevance = relevance
      # symlit for ground eatom replacement
      self.replacement = replacement
      # key = argument position, value = list of ClingoID
      self.predinputs = collections.defaultdict(list)
      # list of all elements in self.predinputs (cache)
      self.allinputs = []
      # whether this should be verified on partial assignments
      self.verify_on_partial = verify_on_partial

  class Nogood:
    def __init__(self):
      self.literals = set()

    def add(self, lit):
      if -lit in self.literals:
        return False
      self.literals.add(lit)
      return True

  class StopPropagation(Exception):
    pass

  def __init__(self, name, pcontext, ccontext, eaeval, partial_evaluation_eatoms):
    self.name = 'ClingoProp('+name+'):'
    # key = eatom
    # value = list of EAtomVerification
    self.eatomVerifications = collections.defaultdict(list)
    # mapping from solver literals to lists of strings
    self.dbgSolv2Syms = collections.defaultdict(list)
    # mapping from symbol to solver literal
    self.dbgSym2Solv = {}
    # program context - to get external atoms and signatures to initialize EAtomVerification instances
    self.pcontext = pcontext
    # clasp context - to store the propagator for external atom verification
    self.ccontext = ccontext
    # helper for external atom evaluation - to perform external atom evaluation
    self.eaeval = eaeval
    # list of names of external atoms that should do checks on partial assignments
    self.partial_evaluation_eatoms = partial_evaluation_eatoms

  def init(self, init):
    name = self.name+'init:'
    # register mapping for solver/grounder atoms!
    # no need for watches as long as we use only check()
    require_partial_evaluation = False
    for eatomname, signatures in self.pcontext.eatoms.items():
      logging.info(name+' processing eatom '+eatomname)
      found_this_eatomname = False
      verify_on_partial = eatomname in self.partial_evaluation_eatoms
      for siginfo in signatures:
        logging.debug(name+' init processing eatom {} relpred {} reppred arity {}'.format(
          eatomname, siginfo.relevancePred, siginfo.replacementPred, siginfo.arity))
        for xrep in init.symbolic_atoms.by_signature(siginfo.replacementPred, siginfo.arity):
          found_this_eatomname = True
          logging.debug(name+'   replacement atom {}'.format(str(xrep.symbol)))
          replacement = SymLit(xrep.symbol, init.solver_literal(xrep.literal))
          xrel = init.symbolic_atoms[clingo.Function(name=siginfo.relevancePred, arguments = xrep.symbol.arguments)]
          logging.debug(name+'   relevance atom {}'.format(str(xrel.symbol)))
          relevance = SymLit(xrel.symbol, init.solver_literal(xrel.literal))

          verification = self.EAtomVerification(relevance, replacement, verify_on_partial)

          # get symbols given to predicate inputs and register their literals
          for argpos, argtype in enumerate(dlvhex.eatoms[eatomname].inspec):
            if argtype == dlvhex.PREDICATE:
              argval = str(xrep.symbol.arguments[argpos])
              logging.debug(name+'     argument {} is {}'.format(argpos, str(argval)))
              relevantSig = [ (aarity, apol) for (aname, aarity, apol) in init.symbolic_atoms.signatures if aname == argval ]
              logging.debug(name+'       relevantSig {}'.format(repr(relevantSig)))
              for aarity, apol in relevantSig:
                for ax in init.symbolic_atoms.by_signature(argval, aarity):
                  logging.debug(name+'         atom {}'.format(str(ax.symbol)))
                  predinputid = ClingoID(self.ccontext, SymLit(ax.symbol, init.solver_literal(ax.literal)))
                  verification.predinputs[argpos].append(predinputid)

          verification.allinputs = frozenset(hexlite.flatten([idlist for idlist in verification.predinputs.values()]))
          self.eatomVerifications[eatomname].append(verification)
      if found_this_eatomname:
        # this eatom is used at least once in the search
        if eatomname in self.partial_evaluation_eatoms:
          logging.info('%s will perform checks on partial assignments due to external atom %s', name, eatomname)
          require_partial_evaluation = True

    if require_partial_evaluation:
      init.check_mode = clingo.PropagatorCheckMode.Fixpoint
    else:
      # this is the default anyways
      init.check_mode = clingo.PropagatorCheckMode.Total

    # for debugging: get full symbol table
    if __debug__:
      for x in init.symbolic_atoms:
        slit = init.solver_literal(x.literal)
        logging.debug("PropInit symbol:{} lit:{} isfact:{} slit:{}".format(x.symbol, x.literal, x.is_fact, slit))
        prefix = 'F'
        if not x.is_fact:
          prefix = str(x.literal)
        self.dbgSolv2Syms[slit].append(prefix+'/'+str(x.symbol))
        self.dbgSym2Solv[x.symbol] = slit

    # WONTFIX (near future) implement this current type of check in on_model where we can comfortably add all nogoods immediately
    # TODO (near future) use partial checks and stay in check()
    # TODO (far future) create one propagator for each external atom (or even for each external atom literal)
    #                   which watches predicate inputs, relevance, and replacement, and incrementally finds when it should compute
    #                   [then we need to find out which grounded input tuple belongs to which atom, so we might need
    #                    nonground-eatom-literal-unique input tuple auxiliaries (which might hurt efficiency)]
  def check(self, control):
    '''
    * get valueAuxTrue and valueAuxFalse truth values
    * get predicate input truth values/extension
    * for each true/false external atom call the plugin and add corresponding nogood
    '''
    # called on total assignments (even without watches)
    name = self.name+'check:'
    logging.info('%s entering with assignment.is_total=%d', self.name, control.assignment.is_total)
    #for t in traceback.format_stack():
    #  logging.info(self.name+'   '+t)
    if __debug__:
      true = []
      false = []
      unassigned = []
      for slit, syms in self.dbgSolv2Syms.items():
        info = "{}={{{}}}".format(slit, ','.join(syms))
        if control.assignment.is_true(slit):
          true.append(info)
        elif control.assignment.is_false(slit):
          false.append(info)
        else:
          assert(control.assignment.value(slit) == None)
          unassigned.append(info)
      if len(true) > 0: logging.debug(name+" assignment has true slits "+' '.join(true))
      if len(false) > 0: logging.debug(name+" assignment has false slits "+' '.join(false))
      if len(unassigned) > 0: logging.debug(name+" assignment has unassigned slits "+' '.join(unassigned))
      logging.info(name+"assignment is "+' '.join([ str(x[0]) for x in self.dbgSym2Solv.items() if control.assignment.is_true(x[1]) ]))
    partial_evaluation = not control.assignment.is_total
    with self.ccontext(control, self):
      try:
        for eatomname, veriList in self.eatomVerifications.items():
          for veri in veriList:
            if partial_evaluation and not veri.verify_on_partial:
              # just skip this verification here
              continue
            if control.assignment.is_true(veri.relevance.lit):
              self.verifyTruthOfAtom(eatomname, control, veri)
            else:
              logging.debug(name+' no need to verify atom {}'.format(veri.replacement.sym))
      except ClingoPropagator.StopPropagation:
        # this is part of the intended behavior
        logging.debug(name+' aborted propagation')
        #logging.debug('aborted from '+traceback.format_exc())
    logging.info(self.name+' leaving')

  def verifyTruthOfAtom(self, eatomname, control, veri):
    name = self.name+'vTOA:'
    targetValue = control.assignment.is_true(veri.replacement.lit)
    if __debug__:
      idebug = pprint.pformat([ x.value() for x in veri.allinputs if x.isTrue() ])
      logging.debug(name+' checking if {} = {} with interpretation {} ({})'.format(
        str(targetValue), veri.replacement.sym, idebug,
        {True:'total', False:'partial'}[control.assignment.is_total]))
    holder = dlvhex.eatoms[eatomname]
    # in replacement atom everything that is not output is relevant input
    replargs = veri.replacement.sym.arguments
    inputtuple = tuple(replargs[0:len(replargs)-holder.outnum])
    outputtuple = tuple(replargs[len(replargs)-holder.outnum:len(replargs)])
    logging.debug(name+' inputtuple {} outputtuple {}'.format(repr(inputtuple), repr(outputtuple)))
    outKnownTrue, outUnknown = self.eaeval.evaluate(holder, inputtuple, veri.allinputs)
    logging.debug(name+" outputtuple {} outTrue {} outUnknown {}".format(pprint.pformat(outputtuple), pprint.pformat(outKnownTrue), pprint.pformat(outUnknown)))

    if outputtuple in outUnknown:
      # cannot verify
      logging.info("%s external atom gave tuple %s as unknown -> cannot verify", name, outputtuple)
      return

    realValue = outputtuple in outKnownTrue
    # TODO now handle all outputs in out!
    if realValue == targetValue:
      logging.info("%s atom %s positively verified!", name, eatomname)
      # TODO somehow adding the (redundant) nogood aborts the propagation
      # this was the case with bb7ab74
      # benjamin said there is a bug, now i try the WIP branch 83038e
      return
    else:
      logging.info("%s atom %s verification failed!", name, eatomname)
    # add clause that ensures this value is always chosen correctly in the future
    # clause contains veri.relevance.lit, veri.replacement.lit and negation of all atoms in

    # build nogood: solution is eliminated if ...

    # XXX make this more elegant (not carry everything twice in nogood and in hr_nogood)
    nogood = self.Nogood()
    hr_nogood = []

    # ... all inputs are as they were above ...
    for atom in veri.allinputs:
      # TODO exclude inputs fixed on the top level?
      value = control.assignment.value(atom.symlit.lit)
      if value == True:
        hr_nogood.append( (atom.symlit.sym,True) )
        if not nogood.add(atom.symlit.lit):
          logging.debug(name+" cannot build nogood (opposite literals)!")
          return
      elif value == False:
        hr_nogood.append( (atom.symlit.sym,False) )
        if not nogood.add(-atom.symlit.lit):
          logging.debug(name+" cannot build nogood (opposite literals)!")
          return
      # None case does not contribute to nogood

    checklit = None
    if realValue == True:
      # ... and computation gave true but eatom replacement is false
      checklit = -veri.replacement.lit
      hr_nogood.append( (veri.replacement.sym,False) )
    else:
      # ... and computation gave false but eatom replacement is true
      checklit = veri.replacement.lit
      hr_nogood.append( (veri.replacement.sym,True) )

    if not nogood.add(checklit):
      logging.debug(self.name+"CPvTOA cannot build nogood (opposite literals)!")
      return

    if logging.getLogger().isEnabledFor(logging.INFO):
      hr_nogood_str = repr([ {True:'',False:'-'}[sign]+str(x) for x, sign in hr_nogood ])
      logging.info("%s CPcheck adding nogood %s", name, hr_nogood_str)
    self.addNogood(nogood)

  def addNogood(self, nogood):
    name = self.name+'addNogood:'
    nogood = list(nogood.literals)
    if __debug__:
      logging.debug(name+" adding {}".format(repr(nogood)))
      for slit in nogood:
        a = abs(slit)
        logging.debug(name+"  {} ({}) is {}".format(a, self.ccontext.propcontrol.assignment.value(a), repr(self.dbgSolv2Syms[a])))
    may_continue = self.ccontext.propcontrol.add_nogood(nogood, tag=False, lock=True)
    logging.debug(name+" may_continue={}".format(repr(may_continue)))
    if may_continue == False:
      raise ClingoPropagator.StopPropagation()

class ModelReceiver:
  def __init__(self, facts, config, flpchecker):
    self.facts = set(self._normalizeFacts(facts))
    self.config = config
    self.flpchecker = flpchecker

  def __call__(self, mdl):
    if not self.flpchecker.checkModel(mdl):
      logging.debug('leaving on_model because flpchecker returned False')
      return
    costs = mdl.cost
    if len(costs) > 0 and not mdl.optimality_proven:
      logging.info('not showing suboptimal model (like dlvhex2)!')
      return
    syms = mdl.symbols(atoms=True,terms=True)
    strsyms = [ str(s) for s in syms ]
    if self.config.nofacts:
      strsyms = [ s for s in strsyms if s not in self.facts ]
    if not self.config.auxfacts:
      strsyms = [ s for s in strsyms if not s.startswith(aux.Aux.PREFIX) ]
    if len(costs) > 0:
      # first entry = highest priority level
      # last entry = lowest priority level (1)
      logging.debug('on_model got cost'+repr(costs))
      pairs = [ '[{}:{}]'.format(p[1], p[0]+1) for p in enumerate(reversed(costs)) if p[1] != 0 ]
      costs=' <{}>'.format(','.join(pairs))
    else:
      costs = ''
    sys.stdout.write('{'+','.join(strsyms)+'}'+costs+'\n')

  def _normalizeFacts(self, facts):
    def normalize(x):
      if isinstance(x, shp.alist):
        if x.right == '.':
          assert(x.left == None and x.sep == None and len(x) == 1)
          ret = normalize(x[0])
        else:
          ret = x.sleft()+x.ssep().join([normalize(y) for y in x])+x.sright()
      elif isinstance(x, list):
        ret = ''.join([normalize(y) for y in x])
      else:
        ret = str(x)
      #logging.debug('normalize({}) returns {}'.format(repr(x), repr(ret)))
      return ret
    return [normalize(f) for f in facts]

def execute(pcontext, rewritten, facts, plugins, config):
  # prepare contexts that are for this program but not yet specific for a clasp solver process
  # (multiple clasp solvers are used for finding compatible sets and for checking FLP property)

  # preparing clasp context which does not hold concrete clasp information yet
  # (such information is added during propagation)
  ccontext = ClaspContext()

  # preparing evaluator for external atoms which needs to know the clasp context
  #eaeval = EAtomEvaluator(ccontext)
  eaeval = CachedEAtomEvaluator(ccontext)

  # find names of external atoms that advertises to do checks on a partial assignment
  partial_evaluation_eatoms = [ eatomname for eatomname, info in dlvhex.eatoms.items() if info.props.provides_partial ]
  # XXX we could filter here to reduce this set or we could decide to do no partial evaluation at all or we could do this differently for FLP checker and Compatible Set finder
  should_do_partial_evaluation_on = partial_evaluation_eatoms

  propagatorFactory = lambda name: ClingoPropagator(name, pcontext, ccontext, eaeval, should_do_partial_evaluation_on)

  if config.flpcheck == 'explicit':
    flp_checker_factory = flp.ExplicitFLPChecker
  else:
    assert(config.flpcheck == 'none')
    flp_checker_factory = flp.DummyFLPChecker
  flpchecker = flp_checker_factory(propagatorFactory)

  # TODO get settings from commandline
  cmdlineargs = []
  if config.number != 1:
    cmdlineargs.append(str(config.number))
  # just in case we need optimization
  cmdlineargs.append('--opt-mode=optN')
  cmdlineargs.append('--opt-strategy=usc,9')

  logging.info('sending nonground program to clingo control')
  cc = clingo.Control(cmdlineargs)
  sendprog = shp.shallowprint(rewritten)
  try:
    logging.debug('sending program ===\n'+sendprog+'\n===')
    cc.add('base', (), sendprog)
  except:
    raise Exception("error sending program ===\n"+sendprog+"\n=== to clingo:\n"+traceback.format_exc())

  # preparing context for instantiation
  # (this class is specific to the gringo API)
  logging.info('grounding with gringo context')
  ccc = GringoContext(eaeval)
  flpchecker.attach(cc)
  cc.ground([('base',())], ccc)

  logging.info('preparing for search')

  # name of this propagator CSF = compatible set finder
  checkprop = propagatorFactory('CSF')
  cc.register_propagator(checkprop)
  mr = ModelReceiver(facts, config, flpchecker)

  logging.info('starting search')
  cc.solve(on_model=mr)

  # TODO return code for unsat/sat/opt?
  return 0

