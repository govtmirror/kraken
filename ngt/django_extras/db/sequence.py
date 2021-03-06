from django import db

class SequenceSource(object):
    """ Abstract base class for Sequence Sources """
    def __init__(self, connection, name):
        self.name = name
        self.connection = connection
        if not self._sequence_exists():
            self._create_sequence()
            
class SqliteSequenceSource(SequenceSource):

    # tablename = 'seq_transaction_id'
    
    def nextval(self):
        #incr and return value
        cur = self.connection.cursor()
        cur.execute('SELECT max(id) from %s' % self.name)
        value = cur.fetchone()[0]
        value += 1
        cur.execute('UPDATE %s SET id = %d;' % (self.name, value))
        cur.close()
        return value
        
    def currval(self):
        #return value without incrementing
        cur = self.connection.cursor()
        cur.execute('SELECT max(id) from %s' % self.name)
        value = cur.fetchone()[0]
        cur.close()
        return value
        
    def setval(self, value):
        cur = self.connection.cursor()
        cur.execute('UPDATE %s SET id = %d;' % (self.name, value))
        cur.close()
        return True
        
    def _sequence_exists(self):
        # return true if the sequence or table exists
        cursor = self.connection.cursor()
        try:
            cursor.execute("SELECT id from %s LIMIT 1" % self.name)
            return True
        except db.backend.Database.OperationalError:
            return False
    
    def _create_sequence(self):
        # create the sequence or table
        cur = self.connection.cursor()
        cur.execute('CREATE TABLE %s (id integer default 1)' % self.name)
        cur.execute('INSERT INTO %s DEFAULT VALUES' % self.name)
        cur.close()
        
        
class PostgresSequenceSource(SequenceSource):

    # sequence_name = 'seq_transaction_id'

    def nextval(self):
        #incr and return value[0]
        cur = self.connection.cursor()
        cur.execute("SELECT nextval('%s')" % self.name)
        value = cur.fetchone()[0]
        cur.close()
        return value
        
    def currval(self):
        #return value without incrementing
        cur = self.connection.cursor()
        cur.execute("SELECT last_value from %s" % self.name)
        value = cur.fetchone()[0]
        cur.close()
        return value
        
    def setval(self, value):
        cur = self.connection.cursor()
        cur.execute("SELECT setval('%s', %d, false)" % (self.name, value) )
        cur.close()
        return True
        
    def _sequence_exists(self):
        # return true if the sequence or table exists
        cur = self.connection.cursor()
        try:
            # get the currval and return True
            cur.execute("SELECT last_value from %s" % self.name)
            cur.fetchone()
            cur.close()
            return True
        except db.backend.Database.ProgrammingError:
            cur.close()
            return False
    
    def _create_sequence(self):
        # create the sequence or table
        cur = self.connection.cursor()
        cur.execute("CREATE SEQUENCE %s" % self.name)
        cur.close()
        
class Sequence(object):
    
    def __init__(self, name):
        self._value = None
        backend = db.backend # backend type is defined by django settings
        connection = db.connection
        print "Connection: ", str(connection)
        if 'postgresql' in backend.__name__:
            self.seq_source = PostgresSequenceSource(connection, name)
        elif 'sqlite' in backend.__name__:
            self.seq_source = SqliteSequenceSource(connection, name)
        else:
            raise Exception("Invalid DB backend: %s" % backend.__name__)
            
    def nextval(self):
        self._value = self.seq_source.nextval()
        return self._value
        
    def currval(self):
        if not self._value and self._value != 0:
            self.value = self.seq_source.currval()
        return self._value
        
    def setval(self, value):
        self._value = None
        self.seq_source.setval(value)
        return True
        
    def reset(self):
        return self.setval(1)
