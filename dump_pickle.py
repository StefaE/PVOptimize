import pickle

file = open('./pvcontrol.pickle', 'rb')
data = pickle.load(file)
print(data)
